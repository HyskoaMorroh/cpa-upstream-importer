"""批量探测：站级并行 + 进度回调"""

from __future__ import annotations
import concurrent.futures
from typing import TYPE_CHECKING, Callable
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
            progress_callback: 进度回调 (current, total, site_url, stats)

        Returns:
            {url: CandidateResult} 映射
        """
        results = {}
        total = len(rows)
        current = 0

        def probe_one(row: ParsedRow) -> tuple[tuple[str, str], Any]:
            result = self._prober.probe(row)
            # 键必须含 api_key —— 只用 bare 会让同一个站的多个 Key 互相覆盖。
            # 实测那份配置里 relay-f 与 relay-l 各有 15 个 Key，用 bare 做键
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
                    if progress_callback:
                        progress_callback(
                            current, total, url, dict(self._stats)
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


def existing_weights(cfg: dict) -> dict[tuple[str, str], int]:
    """既有条目的 weight，按 (host, api_key) 索引。只收显式写了的。

    为什么单独一个函数而不塞进 extract_existing_entries 的返回值：那个函数的
    四元组已被调用方与测试依赖，改结构要连带改几处；而这里只需要一张查表。

    为什么必须有（2026-09-01 审计发现）：`weight: 0` 是用户显式表达「把这个站
    逐出调度池」的唯一手段，CPA 缺这个字段时默认 1。全量重建不搬运它 =
    手工封禁的站全部复活，且没有任何提示。

    键用 host 而非 base_url：同一个站在不同段的 base-url 形态不同
    （codex/compat 带 /v1），用 base_url 查不到。
    """
    from .parse import host_of

    out: dict[tuple[str, str], int] = {}
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
                out[(h, k)] = w

    # compat 段的 weight 在 api-key-entries 的每一项上，不在 provider 级
    for prov in cfg.get("openai-compatibility") or []:
        if not isinstance(prov, dict):
            continue
        h = host_of(str(prov.get("base-url") or ""))
        for ke in prov.get("api-key-entries") or []:
            if not isinstance(ke, dict):
                continue
            w = ke.get("weight")
            if not isinstance(w, int):
                continue
            k = str(ke.get("api-key") or "")
            if h and k:
                out[(h, k)] = w

    return out
