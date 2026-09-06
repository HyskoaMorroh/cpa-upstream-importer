"""批量探测：站级并行 + 进度回调"""

from __future__ import annotations
import concurrent.futures
from typing import TYPE_CHECKING, Any, Callable
if TYPE_CHECKING:
    from .pipeline import Prober, CandidateResult
    from .parse import ParsedRow


class BatchProber:
    """站级批量探测器

    相比 Prober 自带的候选并行（4段并行 + 多站串行），这里是站级并行：
    - 多个站同时探测（每站内部仍然 4 段并行）
    - 适用于全量重探场景（100+ 站）
    - 带进度回调
    """

    def __init__(self, prober: Prober, max_workers: int = 30):
        """
        Args:
            prober: 已配置的 Prober 实例
            max_workers: 最大并发站数（建议 30-40）
        """
        self._prober = prober
        self._max_workers = max_workers
        # success = 至少一段可用；all_four = 四段全通（success 的子集）。
        # partial 保留但恒为 0：外部调用方（server.py / web）还在读这个键，
        # 直接删会静默变成 KeyError 或 undefined。见 probe_batch 里的口径说明。
        self._stats = {"success": 0, "all_four": 0, "partial": 0, "failure": 0}
        # 抛异常的站：[(站, 原因), ...]。调用方要能说出「哪几个站没跑成」——
        # results 里少一条而不知道为什么，比直接报错更难查。
        # 累加都在 as_completed 那个循环里做，那是单线程，不需要锁。
        self.errors: list[tuple[str, str]] = []

    def probe_batch(
        self,
        rows: list[ParsedRow],
        progress_callback: Callable[[int, int, str, dict], None] | None = None
    ) -> dict[str, CandidateResult]:
        """批量探测多个站

        Args:
            rows: 站点列表（ParsedRow）
            progress_callback: 进度回调 (current, total, site_bare, stats)
                site_bare 是**脱敏**的站裸地址。绝不要往这里传含 api_key
                的值 —— 它会一路进日志、JSON 响应和导出文件。

        Returns:
            {url: CandidateResult} 映射
        """
        results = {}
        total = len(rows)
        current = 0

        def probe_one(row: ParsedRow) -> tuple[tuple[str, str], Any]:
            # 探测前先调一次 callback 占位，触发 Job.mark_unit_start 记录起始。
            # current=0 是占位符，真实进度在 as_completed 循环里给。
            if progress_callback:
                progress_callback(0, 0, row.bare, {})
            result = self._prober.probe(row)
            # 键必须含 api_key —— 只用 bare 会让同一个站的多个 Key 互相覆盖。
            # 实测那份配置里 foxtrot 与 relay-l 各有 15 个 Key，用 bare 做键
            # 时 15 个只剩 1 个，而 _stats 仍报 15 个已完成。
            return ((row.bare, row.api_key), result)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="batch-probe") as executor:

            futures = {executor.submit(probe_one, row): row for row in rows}

            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                try:
                    url, result = future.result()
                    results[url] = result

                    # 更新统计
                    #
                    # 口径（2026-09-01 修正）：`success` = **至少一段可用**，
                    # 因为「这个凭据能不能用」才是操作员要的答案。
                    #
                    # 原来 success 要求四段全通，实测 79 个凭据里只有 1 个
                    # 满足 —— 中转站按类型卖，一个站同时卖满 gemini+codex+
                    # claude+compat 本就罕见。结果界面长期显示「成功 0」，
                    # 而下方日志明明在刷 200，操作员据此以为程序没在跑。
                    # 34 个真正可用的凭据里 33 个被划进了「部分通」。
                    #
                    # 四段全通仍然值得单独看，改用 `all_four` 记，不再占用
                    # 「成功」这个词。
                    usable_count = len(result.usable_sections)
                    if usable_count > 0:
                        self._stats["success"] += 1
                        if usable_count == 4:
                            self._stats["all_four"] += 1
                    else:
                        self._stats["failure"] += 1

                    current += 1

                    # 回调
                    #
                    # 传 `row.bare`（站的裸地址）而**不是** `url` ——
                    # 后者是结果字典的键 `(bare, api_key)`，含**完整明文 key**。
                    #
                    # 2026-09-01 实测泄漏：这个值经 server.py 的 progress 事件
                    # 进日志、进 /api/job 的 JSON、再进导出文件。一份 79 凭据的
                    # 日志里有 74 个完整 key 明文可读，而项目的安全模型写着
                    # 「完整 key 只在内存里，不落日志、不进 JSON 响应」。
                    # 站名足够定位「刚完成哪个」，key 在这里没有任何用途。
                    if progress_callback:
                        progress_callback(
                            current, total, row.bare, dict(self._stats)
                        )

                except Exception as e:                     # noqa: BLE001
                    # 单站抛异常不能让整批停下 —— 175 个站里有一个超时就全废
                    # 不可接受。但**异常本身不能吞掉**：原来这里连 e 都没用，
                    # 于是「哪个站为什么失败」无从得知，而调用方只看到 results
                    # 里少了一条。
                    self._stats["failure"] += 1
                    self.errors.append((row.bare, f"{type(e).__name__}: {e}"))
                    current += 1
                    if progress_callback:
                        progress_callback(
                            current, total, row.bare, dict(self._stats)
                        )

        return results


def entry_scope(section: str, base_url: str) -> str:
    """查表键里的「站」这一维。前三段用 host，compat 段用**含路径**的 provider 身份。

    为什么两段不同（2026-09-04 修）
    ---------------------------
    前三段是「一个 Key 一条条目」，站的身份就是 host —— base-url 除了
    `/v1` 后缀之外没有路径。

    compat 段是「一个 provider 一条条目、多 Key 挂在下面」，而同一台主机
    可以按**路径**挂多个互不相干的 provider（本项目自己的
    `tools/e2e_redetect.py` 假上游正是 `127.0.0.1:PORT/good` 与 `.../gate`）。
    用 host 做键时那两个 provider 的条目互相覆盖：重探 `/good` 会拿到
    `/gate` 的 prefix / headers / name / 窗口值。

    渲染归并（`compat_provider_key`）、per-key 续行（`compat_key_blocks`）、
    孤儿保留（`_orphan_provider_lines`）三处本来就用含路径的键，只有这六张
    查表还在用 host —— 这个函数把它们统一起来。

    生产配置 compat 段同 host 多路径 0 处，所以这是补闸而不是修事故。
    """
    if section == "openai-compatibility":
        from .writeback import compat_provider_key
        return compat_provider_key(base_url)
    from .parse import host_of
    return host_of(base_url)



class CarryTables:
    """既有条目里「探测问不出来、必须原样搬」的那些字段，一次建表、逐段搬运。

    为什么要抽成一个类（2026-09-04）
    ------------------------------
    这八张表原来在两处各写一遍：`server.py` 的 `_api_plan` 循环，与
    `tests/rehearse_real_rebuild.py` 的 `build_plans`。两处分叉的后果实测过
    两次：

      · 演练自己也搬 headers，所以「server 不搬」这个缺陷演练照样对上账
      · 反过来，演练少搬一张表时对不上账会被当成产品缺陷

    更要紧的是**可测性**：内联在循环里时只能靠 AST 断言「这一行在不在」，
    而那挡不住「行还在、传的是空」—— 撤销实验里把 `old = hdrs.get(...)` 改成
    `old = None`，1198 项测试全绿。抽出来之后行为直接测得到。

    每张表少一张的后果（都实测过或读过 CPA 源码确认）：

      weight        `weight: 0` 是「逐出加权调度池」的唯一表达
      proxy-url     必须走代理的站会改成直连，下次请求拿 403
      prefix        `ANT/xxx` 这半边别名全失效（实测 121/121 条目）
      provider name CPA 的 provider_key，改名作废冷却状态与能力缓存（12/13）
      headers       整字段消失（实测 24/24 与 66/66 条目）
      能力开关      原来开着的 websockets 被抹掉
      模型级窗口    客户端按 CPA 内置目录的偏大值定压缩点（实测 8 处）
      模型级其余    手工加的 display-name / thinking 等被抹掉
    """

    __slots__ = ("weights", "proxies", "prefixes", "provider_names",
                 "headers", "toggles", "model_context", "model_extras")

    def __init__(self, cfg: dict):
        self.weights = existing_weights(cfg)
        self.proxies = existing_proxies(cfg)
        self.prefixes = existing_prefixes(cfg)
        self.provider_names = existing_provider_names(cfg)
        self.headers = existing_headers(cfg)
        self.toggles = existing_toggles(cfg)
        self.model_context = existing_model_context(cfg)
        self.model_extras = existing_model_extras(cfg)

    def apply(self, sp, api_key: str) -> None:
        """把原值搬进这个 SectionPlan。就地改，不返回。

        搬运方向按字段分（判据是「这个字段是谁的属性」，见 README 的
        「重探时每个字段以哪一侧为准」）：

          · weight / prefix / provider name / 模型级字段 —— 只搬原值，
            探测不产生这些
          · proxy-url —— 探测有值优先（那是本次实测结论），否则搬原值
          · headers —— 合并，原值为底、探测值覆盖同名键
          · 能力开关 —— 只提供「未探测时的兜底」，覆盖关系在
            `_toggle_lines` 里判，不在这里
        """
        from .writeback import merge_entry_headers

        sec = sp.section
        # 键里的「站」这一维：前三段是 host，compat 段是含路径的 provider
        # 身份 —— 同一主机可按路径挂多个 provider，用 host 查会串到另一个
        # 上游的配置上。见 entry_scope。
        scope = entry_scope(sec, sp.base_url)
        k = (sec, scope, api_key)

        w = self.weights.get(k)
        if w is not None:
            sp.weight = w

        # 探测判定需要代理时它已有值，不覆盖 —— 那是本次实测结论；
        # 只补「原来有、这次没探出来」的情形。
        if not sp.proxy_url:
            got = self.proxies.get(k)
            if got:
                sp.proxy_url = got

        sp.headers = merge_entry_headers(self.headers.get(k), sp.headers)

        got_t = self.toggles.get(k)
        if got_t:
            sp.prior_toggles = dict(got_t)

        # `"" in prefixes` 与「键不存在」要分开 —— 前者是操作员显式写了空串，
        # 也该照原样。
        if k in self.prefixes:
            sp.prefix = self.prefixes[k]

        if sec == "openai-compatibility":
            sp.provider_name = provider_name_for(self.provider_names,
                                                 sp.base_url)

        sp.prior_context = {
            name: val
            for (s2, h2, k2, name), val in self.model_context.items()
            if s2 == sec and h2 == scope and k2 == api_key
        }
        sp.prior_model_extras = {
            name: dict(val)
            for (s2, h2, k2, name), val in self.model_extras.items()
            if s2 == sec and h2 == scope and k2 == api_key
        }


def extract_existing_entries(cfg: dict) -> list[tuple[str, str, str, str]]:
    """从 config.yaml 提取所有既有站

    Returns:
        [(section_short, base_url, api_key, original_section), ...]
        section_short: 'gemini' | 'codex' | 'claude' | 'compat'
        original_section: YAML 段名（如 'gemini-api-key', 'openai-compatibility'）
    """
    # SECTIONS 是元组：('gemini-api-key', 'codex-api-key', 'claude-api-key', 'openai-compatibility')
    SECTION_MAP = {
        "gemini": "gemini-api-key",
        "codex": "codex-api-key",
        "claude": "claude-api-key",
        "compat": "openai-compatibility",
    }

    entries = []

    # Gemini / Codex / Claude
    for section_short, yaml_key in SECTION_MAP.items():
        if section_short == "compat":
            continue  # compat 单独处理
        items = cfg.get(yaml_key, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            base_url = item.get("base-url", "").strip()
            api_key = item.get("api-key", "").strip()
            if base_url and api_key:
                entries.append((section_short, base_url, api_key, yaml_key))

    # OpenAI Compatibility
    compat_items = cfg.get("openai-compatibility", [])
    if isinstance(compat_items, list):
        for provider in compat_items:
            if not isinstance(provider, dict):
                continue
            base_url = provider.get("base-url", "").strip()
            api_keys = provider.get("api-key-entries", [])
            if not isinstance(api_keys, list):
                continue
            for key_entry in api_keys:
                if not isinstance(key_entry, dict):
                    continue
                api_key = key_entry.get("api-key", "").strip()
                if base_url and api_key:
                    entries.append(("compat", base_url, api_key, "openai-compatibility"))

    return entries


def existing_weights(cfg: dict) -> dict[tuple[str, str, str], int]:
    """既有条目的 weight，按 **(段, host, api_key)** 索引。只收显式写了的。

    为什么单独一个函数而不塞进 extract_existing_entries 的返回值：那个函数的
    四元组已被调用方与测试依赖，改结构要连带改几处；而这里只需要一张查表。

    为什么必须有（2026-09-01 审计发现）：`weight: 0` 是用户显式表达「把这个站
    逐出调度池」的唯一手段，CPA 缺这个字段时默认 1。全量重建不搬运它 =
    手工封禁的站全部复活，且没有任何提示。

    为什么键里必须有段（2026-09-03 对账发现，与 existing_proxies 同一个成因）
    ------------------------------------------------------------------
    原来按 (host, api_key) 索引、跨段共用一个值。实测生产 config.yaml：
    facai 的 3 把 Key 在 codex 与 claude 段是 `weight: 0`（那两条路径实测
    静默换模，已封），在 compat 段**故意没写**（那条路径可用）；100xlabs 的
    3 把 Key 同样只在 claude 段封。按两元组搬运会把 0 灌进 compat 段 ——
    6 个 (凭据, 段) 组合被无声封禁。

    `weight: 0` 的后果比多一跳代理重得多：weighted-round-robin 下
    positiveWeightAuths（selector.go:637-644）把它整个剔出候选，那个站在
    那一段直接不参与调度，而 YAML 合法、validate 报成功、写后验证也发现不了。

    键用 host 而非 base_url：同一个站在不同段的 base-url 形态不同
    （codex/compat 带 /v1），用 base_url 查不到。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str], int] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            w = e.get("weight")
            if not isinstance(w, int):          # 缺失或非整数都当没写
                continue
            h = host_of(str(e.get("base-url") or ""))
            k = str(e.get("api-key") or "")
            if h and k:
                out[(section, h, k)] = w

    # compat 段的 weight 在 api-key-entries 的每一项上，不在 provider 级
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            w = ke.get("weight")
            if not isinstance(w, int):
                continue
            k = str(ke.get("api-key") or "")
            if h and k:
                out[("openai-compatibility", h, k)] = w

    return out


def existing_proxies(cfg: dict) -> dict[tuple[str, str, str], str]:
    """既有条目的 proxy-url，按 **(段, host, api_key)** 索引。只收非空值。

    为什么必须搬运（2026-09-02 拿生产 config.yaml 逐字段对账发现）：
    `proxy_url` 只在探测**当场判定需要代理**（IP封/边缘救回）时才有值，
    而重探时那个站可能这次直连就通了 —— 于是方案里 proxy_url 为空，
    整段重写把原有的 26 条 `proxy-url: http://mihomo:7890` 全部抹掉。

    后果不可见：YAML 合法、validate 报成功，但那些必须走代理的站下次
    请求直连、拿 403，而配置里已经没有任何痕迹说明它本来有代理。

    为什么键里必须有段（2026-09-02 二次对账发现）
    ------------------------------------------
    原来按 (host, api_key) 索引，跨段共用一个值。实测 kktoken.cc 的 5 把 Key
    在 compat 段有 `proxy-url: http://mihomo:7890`，在 claude 段**故意没有** ——
    那个站的 claude 路径直连可用，走代理反而多一跳。按两元组搬运会把 compat
    的代理灌进 claude 段，实测 claude 段 proxy-url 从 3 条涨到 8 条。

    多一跳不会让请求失败，所以 validate 与写后验证都发现不了 —— 又是一处
    静默改变行为。段是 proxy-url 的一部分语义，不能跨段共用。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str], str] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            pu = str(e.get("proxy-url") or "").strip()
            if not pu:
                continue
            h = host_of(str(e.get("base-url") or ""))
            k = str(e.get("api-key") or "")
            if h and k:
                out[(section, h, k)] = pu

    # compat 段的 proxy-url 在 api-key-entries 的每一项上，不在 provider 级
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            pu = str(ke.get("proxy-url") or "").strip()
            k = str(ke.get("api-key") or "")
            if pu and h and k:
                out[("openai-compatibility", h, k)] = pu

    return out


def existing_headers(cfg: dict) -> dict[tuple[str, str, str], dict[str, str]]:
    """既有条目的 headers，按 **(段, host, api_key)** 索引。只收非空 map。

    为什么必须搬运（2026-09-04 逐字段对账发现，与 proxy-url 同一个成因）
    ----------------------------------------------------------------
    `headers` 是四段条目级字段里**唯一**「只生成、不搬运」的那一个：
      · 它在 `_RENDERED_KEYS` 里，所以 `extract_carry_lines` 不搬（那是给
        白名单**外**的字段用的）
      · 而 `existing_*` 查表以前没有它
    于是整段重写时原值被方案值整体替换。方案的 headers 只在探测**当场判定
    需要 UA**（`v.need_ua`）时才有值，重探时那个站可能 baseline 就通 ——
    `sp.headers` 是空 dict，那一行整个写不出来。

    实测两份生产 config.yaml：Desktop 版 24/24 条目的 headers 全丢
    （codex 10、claude 13、compat 1），fsdownload 版 66/66 全丢。丢的内容含
    `anthropic-beta`（Desktop 24 条、fsdownload 25 条）、`user-agent`、
    `originator`、X-Stainless 全族。

    后果是真的改运行行为：条目级 headers 会一路到达上游请求
    （config.go 的 addConfigHeadersToAttrs → attribute `header:<Name>` →
    util/header_helpers.go 的 ApplyCustomHeadersFromAttrs），所以丢掉
    `anthropic-beta: context-1m-2025-08-07` 就是把那个站的 1m 上下文关掉，
    而 YAML 合法、validate 报成功、写后验证也发现不了。

    与 `proxy_url` 采用同一条处置：探测有值优先（那是本次实测结论），
    否则搬原值。见 server.py 里 headers 那一段的合并逻辑。

    键含段的理由与 existing_proxies 相同：同一个凭据在不同段的 headers 是
    **独立配置**（claude 段要 anthropic-beta，compat 段发它毫无意义）。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str], dict[str, str]] = {}

    def take(section: str, h: str, k: str, raw) -> None:
        if not isinstance(raw, dict) or not raw:
            return
        got = {str(kk): str(vv) for kk, vv in raw.items()
               if str(kk).strip() and vv is not None}
        if got and h and k:
            out[(section, h, k)] = got

    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            take(section, host_of(str(e.get("base-url") or "")),
                 str(e.get("api-key") or ""), e.get("headers"))

    # compat 段的 headers 在 **provider 级**，组内所有 Key 共用同一份
    # （OpenAICompatibilityAPIKey 只有 api-key / weight / proxy-url，
    # config_types.go:700 起）。所以按组内每把 Key 各存一份同样的值 ——
    # 查表方按 (段, host, key) 问，拿到的是这个 provider 的那一份。
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        hdrs = prov.get("headers")
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            take("openai-compatibility", h, str(ke.get("api-key") or ""), hdrs)

    return out


def existing_prefixes(cfg: dict) -> dict[tuple[str, str, str], str]:
    """既有条目的 prefix，按 **(段, host, api_key)** 索引。含空串。

    为什么必须逐条搬运而不是靠 `dominant_prefix` 猜（2026-09-03 逐字段
    deep-equal 才抓到）：那个函数只在该段 70% 以上统一时才给值，是给**新条目**
    用的默认值。全量重探更新的是既有条目 —— 它自己写的 prefix 才是真的。

    `force-model-prefix: false` 下 prefix 是**额外注册一个命名空间别名**
    （applyModelPrefixes，service_models.go:600-614 对每个模型同时注册
    `claude-opus-5` 与 `ANT/claude-opus-5`）。抹掉它不会让站不可用，但所有按
    `ANT/xxx` 发的请求会命中不到 —— 而客户端侧的模型名往往就是那个别名。

    收空串：`prefix: ""` 与不写在 CPA 侧等价（normalizeModelPrefix 会 trim），
    但「原来显式写了空」与「原来没写」在 diff 上有区别，照原样更干净。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str], str] = {}
    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict) or "prefix" not in e:
                continue
            h = host_of(str(e.get("base-url") or ""))
            k = str(e.get("api-key") or "")
            if h and k:
                out[(section, h, k)] = str(e.get("prefix") or "")

    # compat 段的 prefix 在 provider 级，组内所有 Key 共用
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict) or "prefix" not in prov:
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        val = str(prov.get("prefix") or "")
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            k = str(ke.get("api-key") or "")
            if h and k:
                out[("openai-compatibility", h, k)] = val

    return out


def existing_provider_names(cfg: dict) -> dict[str, str]:
    """compat 段每个 provider 的 `name`，按 **provider 身份**（含路径）索引。

    `name` 就是 CPA 的 provider 身份 ——
    `util.OpenAICompatibleProviderKey(name)` 的结果写进 Auth 的
    `provider_key`，而冷却（conductor_cooldown.go:73）、模型能力
    （api_key_model_capabilities.go:186）、执行路由（conductor_execution.go:1605-1609）
    三处都按它索引。

    实测生产配置里 12/13 个 provider 的 name 是人读短名（`runanytime`、
    `chma`、`facai`），与 host 不同。用 host 现编会把它们全部改名：冷却状态
    与能力缓存作废，而且本项目自己的 `name_alias_map`（注释里的短名 → 域名）
    也跟着失效 —— 下一轮读注释拿健康度就大面积漏判。

    为什么键是 `compat_provider_key` 而不是 host（2026-09-04 修）
    -------------------------------------------------------
    同一台主机可以按**路径**挂多个互不相干的 provider。按 host 索引时后一个
    覆盖前一个，于是重探 `/good` 会拿到 `/gate` 的 name —— 两个 provider 同名，
    CPA 的三处索引对同一把 Key 命中两套配置，同一把 Key 在轮询池里占两个位。

    这与 `compat_key_blocks` / `_orphan_provider_lines` / 渲染时的归并键
    （都是 `compat_provider_key`）本来就该一致 —— 这一处是漏改的。
    生产配置 compat 段同 host 多路径 0 处，但本项目自己的
    `tools/e2e_redetect.py` 假上游正是这个形态。

    **兼容回落**：调用方拿到的是三元组查不到时按 host 再查一次的两级表 ——
    见 `provider_name_for`。直接读这张表的调用方要用那个函数，别自己拼键。
    """
    out: dict[str, str] = {}
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        pk = entry_scope("openai-compatibility",
                         str(prov.get("base-url") or ""))
        nm = str(prov.get("name") or "").strip()
        if pk and nm:
            out[pk] = nm
    return out


def provider_name_for(names: dict[str, str], base_url: str) -> str:
    """从 `existing_provider_names` 的表里取这个 base-url 对应的 provider name。

    两级查找：先按 `compat_provider_key`（含路径）精确匹配，查不到再按 host
    回落。回落的理由：新导入的站在表里没有精确条目，而「同一个 host 只有一个
    provider」是绝大多数情形 —— 那时按 host 拿到的就是对的。

    同 host 有多个 provider 时回落会**不确定**（拿到哪个取决于 dict 顺序），
    所以只在精确查不到时才用，且此时 host 下只可能是「新站」——
    既有站必然精确命中。
    """
    from .parse import host_of

    pk = entry_scope("openai-compatibility", base_url)
    if pk in names:
        return names[pk]
    h = host_of(base_url)
    for key, nm in names.items():
        if host_of(key) == h:
            return nm
    return ""


def existing_toggles(cfg: dict) -> dict[tuple[str, str, str], dict[str, bool]]:
    """既有条目里**显式写了 true** 的段专属能力开关，按 (段, host, api_key) 索引。

    收哪两个：
        codex-api-key         websockets                Responses 的 WS 通道
        openai-compatibility  support-prompt-cache-key  注入 prompt_cache_key

    为什么只收 true：CPA 的零值就是关闭（`websockets bool` 无指针，
    config_types.go:486），`false` 与「不写」运行时完全等价。只收 true 让
    搬运的语义变成「把用户显式打开的开关保住」，而不需要区分两种关闭写法。

    为什么必须搬（2026-09-04，与 headers 同一个成因）：这两个字段在
    `_RENDERED_KEYS` 里，所以 `extract_carry_lines` 不搬（那是给白名单**外**的
    字段用的）；而方案侧的值只在**本次探测跑了能力探测**时才有。关掉
    `--no-capabilities` 或该段本次判不可用时方案是 None，整段重写就把原有的
    `websockets: true` 抹掉了。实测生产配置 codex 段有 2 条。

    另两段没有这类开关：GeminiKey / ClaudeKey 的布尔字段
    （`rebuild-mid-system-message` / `experimental-cch-signing` /
    `disable-cooling`）是**本地行为**开关而不是上游能力，它们由 carry 原文
    搬运（不在 `_RENDERED_KEYS` 里），不需要这张表。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str], dict[str, bool]] = {}

    for e in cfg.get("codex-api-key") or []:
        if not isinstance(e, dict):
            continue
        if e.get("websockets") is not True:
            continue
        h = host_of(str(e.get("base-url") or ""))
        k = str(e.get("api-key") or "")
        if h and k:
            out.setdefault(("codex-api-key", h, k), {})["websockets"] = True

    # compat 的开关在 **provider 级**，组内所有 Key 共用 —— 按每把 Key 各存
    # 一份同样的值，查表方按 (段, host, key) 问就拿得到（与 existing_headers
    # 同一套做法）。
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        if prov.get("support-prompt-cache-key") is not True:
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            k = str(ke.get("api-key") or "")
            if h and k:
                out.setdefault(("openai-compatibility", h, k), {})[
                    "support-prompt-cache-key"] = True

    return out


def existing_model_extras(cfg: dict) -> dict[tuple[str, str, str, str], dict]:
    """既有条目里每个模型的**白名单外字段**，按 (段, host, api_key, 模型名) 索引。

    白名单是 render_entry 自己有确定值的两个：`name` 与
    `max-context-length`（后者另有 `existing_model_context` 专门搬）。
    `alias` **不在**白名单里（2026-09-04 修）—— render_entry 写死 `alias: ""`，
    而那只对「原本就是空串」的条目成立：非空 alias 三方都不管
    （render 写死、这张表原来排除它、carry 跳过 models 块），整段重写后
    `alias: "claude-opus-5"` 变成 `alias: ""`。两份生产文件里非空 alias 是 0 个，
    所以当前是潜在缺陷；但 README 明确把 `models[].alias` 写成推荐做法
    （「保留原名轮询，用 models[].alias 补段级兼容名」），按文档配就会丢。

    CPA 的模型条目还支持另外七个
    （config_types.go 的 ClaudeModel / CodexModel / GeminiModel /
    OpenAICompatibilityModel）：

        display-name        模型目录里显示的人读名
        force-mapping       把上游响应的 model 字段改写回 alias
        image               图像模型，注册成 OpenAIImageModelType
        input-modalities    支持的输入模态
        output-modalities   支持的输出模态
        is-compat           走兼容口
        thinking            思考档位支持（不写时 CPA 默认给 low/medium/high）

    为什么单独一份（2026-09-03 逐字段核对 CPA 结构体发现）：`extract_carry_lines`
    有意跳过整个 models 块，而 render_entry 只写那三个 —— 剩下七个一律抹掉。
    当前这份生产 config.yaml 一个都没用到，所以还没踩到；但那是巧合，
    手工加一个 `thinking:` 或 `image: true` 之后整段重写就会静默丢掉它，
    而 YAML 合法、validate 报成功。

    值存**解析后的 dict**而不是原文行：这里的字段结构浅（标量与字符串数组），
    重新序列化不难；而按原文行搬要处理「模型清单变了、原行还在」的错位。
    """
    from .parse import host_of

    # alias 不在这里 —— 它要进 extras 才能被搬回来。见上面的说明。
    KNOWN = {"name", "max-context-length"}
    out: dict[tuple[str, str, str, str], dict] = {}

    def take(section: str, h: str, k: str, models) -> None:
        for m in models or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "").strip()
            extra = {kk: vv for kk, vv in m.items() if kk not in KNOWN}
            if name and extra:
                out[(section, h, k, name)] = extra

    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            take(section, host_of(str(e.get("base-url") or "")),
                 str(e.get("api-key") or ""), e.get("models"))

    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if isinstance(ke, dict):
                take("openai-compatibility", h,
                     str(ke.get("api-key") or ""), prov.get("models"))

    return out


# 旧版 `_bisect` 返回的是**字符数**而不是 token 数（2026-09-06 修的单位错误），
# 于是它写进 config.yaml 的 `max-context-length` 虚高约 4 倍。那些值现在还躺在
# 文件里，而 `existing_model_context` 会把它们原样搬回来 —— 不识别就等于把
# 一个已知的错误值一轮轮传下去。
#
# 识别办法：旧二分的取值集合是**封闭且可枚举的** —— lo=200000、hi=1100000、
# 四轮二分，全部可能返回值只有 17 个（200000、256250、…、987500、1043750、
# 1100000，公差 56250 的等差数列）。真实的上游自报窗口不会恰好落在这个格子上
# （实测那份配置里 15515 就不在格子里，987500 在）。
#
# 命中格子的值按同一个系数折回 token；不在格子里的原样保留 —— 那是上游正文
# 自报的 token 数，本来就是对的。
#
# **200000 有意排除**：它既是旧二分的下界，也是现役模型里最常见的真实窗口
# （Claude 与 GPT 两系都正好 200k token）。把它当旧值折成 5 万会把一个正确的
# 声明值改错，而误差方向是「报小」—— 那会让客户端过早压缩，是真实损失。
# 反过来把一个旧的 20 万字符值留着，误差方向是「报大 4 倍」，但只在
# 「这个站真的只能吃 20 万字符」时才发生，而那种站会在正文里自报窗口
# （那条路径不经二分）。两害相权取其轻。
_LEGACY_CHAR_GRID = frozenset(
    {1_100_000} | {200_000 + 56_250 * i for i in range(1, 16)}
)


def _fix_legacy_char_context(val: int) -> int:
    """把旧版按字符写下的窗口值折算成 token。不是旧值就原样返回。

    折算系数与 `pipeline._CHARS_PER_TOKEN` 必须一致 —— 从那里导入，
    不在这里再写一个 4（两处各写一份的分叉在本项目发生过多次）。
    """
    if val not in _LEGACY_CHAR_GRID:
        return val
    from .pipeline import _chars_to_tokens
    return _chars_to_tokens(val)


def existing_model_context(cfg: dict) -> dict[tuple[str, str, str, str], int]:
    """既有条目里每个模型自己的 `max-context-length`，按
    **(段, host, api_key, 模型名)** 索引。只收正整数。

    为什么必须搬（2026-09-03 逐字段对账发现）：这个值在 `models:` 块**里面**，
    而 `extract_carry_lines` 有意跳过整个 models 块（模型清单由方案重新生成，
    搬原文行会与新清单打架）。于是它落进一个空档：
      · carry 不搬 —— 在 models 块内
      · 方案只带**一个**值（`sp.max_context_length` + `sp.context_model`），
        那是本次探测实测的那一个模型
    结果：本次没探上下文（`--no-context`、或那个模型没被验）时，历史实测值
    全部消失。实测生产配置有 8 处，kktoken.cc 的 987500 与 zzzcoding 的 15515
    都在其中。

    丢了的后果不是不可用，而是**客户端按错的窗口定压缩点**：CPA 把它写进
    `/v1/models` 的 `context_length`（model_registry.go:1437-1438）与 Codex 的
    `max_context_window`（internal/client/codex/models/models.go:208-210）。
    没有这个值时 CPA 回落内置目录值 —— 那对中转站往往偏大，客户端塞满上下文
    才发现被上游截断，正是第 08 章那条 400 的成因。

    键含模型名：同一个条目里不同模型的窗口能差一个数量级（opus 与 haiku），
    把其中一个的值抄给另一个比丢掉更糟。
    """
    from .parse import host_of

    out: dict[tuple[str, str, str, str], int] = {}

    def take(section: str, h: str, k: str, models) -> None:
        for m in models or []:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "").strip()
            val = m.get("max-context-length")
            if name and isinstance(val, int) and val > 0:
                # 旧版写下的字符数在这里折回 token。见 _fix_legacy_char_context。
                out[(section, h, k, name)] = _fix_legacy_char_context(val)

    for section in ("gemini-api-key", "codex-api-key", "claude-api-key"):
        for e in cfg.get(section) or []:
            if not isinstance(e, dict):
                continue
            take(section, host_of(str(e.get("base-url") or "")),
                 str(e.get("api-key") or ""), e.get("models"))

    # compat 段的 models 在 provider 级，组内所有 Key 共用同一份
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = entry_scope("openai-compatibility",
                        str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if isinstance(ke, dict):
                take("openai-compatibility", h,
                     str(ke.get("api-key") or ""), prov.get("models"))

    return out
