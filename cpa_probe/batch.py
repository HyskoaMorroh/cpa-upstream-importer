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
        self._stats = {"success": 0, "partial": 0, "failure": 0}

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

        def probe_one(row: ParsedRow) -> tuple[str, Any]:
            result = self._prober.probe(row)
            return (row.bare, result)

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
                    usable_count = len(result.usable_sections)
                    if usable_count == 4:
                        self._stats["success"] += 1
                    elif usable_count > 0:
                        self._stats["partial"] += 1
                    else:
                        self._stats["failure"] += 1

                    current += 1

                    # 回调
                    if progress_callback:
                        progress_callback(
                            current, total, url, dict(self._stats)
                        )

                except Exception as e:
                    # 探测失败，记录为 failure
                    self._stats["failure"] += 1
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
