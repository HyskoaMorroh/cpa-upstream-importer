"""同代兄弟不许被次版本挤掉（修改要求 3⑵②③）。

用户原话（要求③）：「所有相同等级系列的模型全部都要勾选上，检测的时候发现
如勾选的了 gpt-5.6 却没有勾选 gpt-5.6-sol 等这种重大失误」；要求②：「如 codex
当前最新模型为 gpt-6 系列**所有模型名称**」。

现场（2026-09-30 本机实跑，CPA 权威名录 90 个模型）：
    codex 段 section_allows 放行 8 个
      gpt-6-luna  gpt-6-astra  gpt-6-sol  gpt-6.1-sol
      gpt-5.6-sol gpt-5.6-luna gpt-5.6-terra gpt-5.5
    newest_generation_per_line 只选出 1 个：gpt-6.1-sol

根因：阶段 B 按 `_product_line` 分组比完整世代，而 `_product_line` 把 gpt 的
全部后缀（-astra / -sol / -luna）都剥掉、归成同一条线 `gpt`。于是
`gpt-6.1-sol` 的次版本 1 把同属最新主版本的 `gpt-6-astra`、`gpt-6-luna`
一起判成「低世代」淘汰掉 —— 恰是要求③点名的「同级系列没有全勾」。

`_product_line` 的 docstring 说明它刻意剥掉 `-sol`/`-luna`/`-terra`，理由是
「那三个是同一代的三个变体，不该占三个**轮转位**」。那是给 `_round_robin`
的配额判据，不是世代判据 —— 两个用途共用一个函数才出这个 bug。
世代收敛要用 `series_and_version` 的**模板**（`gpt-*-astra` / `gpt-*-sol`
各自成线），轮转配额仍用 `_product_line`（粗线，避免一族占满前 N）。
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import model_catalog


# 2026-09-30 实测的 CPA 权威名录 codex 段放行集。
CODEX_ALLOWED = [
    "gpt-6-luna", "gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-5.6-sol",
]


class SameGenerationSiblingsTests(unittest.TestCase):
    def test_newest_major_siblings_all_survive(self):
        """gpt-6 的三条线都要留下，不能被 gpt-6.1-sol 的次版本抹平。"""
        kept = model_catalog.newest_generation_per_line(CODEX_ALLOWED)
        self.assertIn("gpt-6-astra", kept)
        self.assertIn("gpt-6-luna", kept)
        # 同一条线（-sol）内仍取最高世代：6.1 > 6，所以 gpt-6-sol 该被压掉。
        self.assertIn("gpt-6.1-sol", kept)
        self.assertNotIn("gpt-6-sol", kept)

    def test_older_major_is_still_dropped(self):
        """阶段 A 的族内主版本比较不受影响：gpt-5.x 全淘汰。"""
        kept = model_catalog.newest_generation_per_line(CODEX_ALLOWED)
        for stale in ("gpt-5.5", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra"):
            self.assertNotIn(stale, kept)

    def test_bare_and_suffixed_same_generation_both_kept(self):
        """要求③的原例：勾了 gpt-6 就必须同时勾 gpt-6-sol。"""
        kept = model_catalog.newest_generation_per_line(["gpt-6", "gpt-6-sol"])
        self.assertEqual(sorted(kept), ["gpt-6", "gpt-6-sol"])

    def test_gemini_selection_is_unchanged(self):
        """gemini 段（-pro / -pro-preview / -pro-low）结果不得回退。"""
        allowed = ["gemini-2.5-pro", "gemini-3-pro-preview",
                   "gemini-3.1-pro-preview", "gemini-3-pro",
                   "gemini-3.1-pro", "gemini-3.1-pro-low"]
        kept = model_catalog.newest_generation_per_line(allowed)
        self.assertIn("gemini-3.1-pro", kept)
        self.assertIn("gemini-3.1-pro-preview", kept)
        self.assertNotIn("gemini-3-pro", kept)
        self.assertNotIn("gemini-2.5-pro", kept)
        # 阶段 C 的算力档收敛仍然生效。
        self.assertNotIn("gemini-3.1-pro-low", kept)

    def test_claude_lines_stay_separate_and_newest_minor_wins(self):
        """claude 的三条线各自比世代；同线内 5-5 压掉 5。"""
        allowed = ["claude-opus-5", "claude-opus-5-5", "claude-sonnet-5",
                   "claude-sonnet-5-5", "claude-fable-5", "claude-fable-5-1",
                   "claude-opus-4-8"]
        kept = model_catalog.newest_generation_per_line(allowed)
        self.assertEqual(
            sorted(kept),
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"])

    def test_gpt_4o_still_loses_to_newer_major(self):
        """阶段 A 的老教训不能回退：gpt-4o 不得与 gpt-6 并存。"""
        kept = model_catalog.newest_generation_per_line(["gpt-4o", "gpt-6-sol"])
        self.assertEqual(kept, ["gpt-6-sol"])

    def test_round_robin_still_groups_by_coarse_product_line(self):
        """轮转配额仍按粗产品线 —— 不能让一族占满前 N 个。"""
        allowed = CODEX_ALLOWED
        out, _why = model_catalog.latest_models(
            "codex-api-key", cfg={}, remote=allowed, limit=2)
        self.assertEqual(len(out), 2)
        for name in out:
            self.assertTrue(name.startswith("gpt-6"), name)

    def test_registration_keeps_every_newest_sibling(self):
        """写进 config.yaml 的注册清单（limit=0）必须含全部同代兄弟。"""
        out, _why = model_catalog.latest_models(
            "codex-api-key", cfg={}, remote=CODEX_ALLOWED, limit=0,
            for_registration=True)
        self.assertEqual(sorted(out),
                         ["gpt-6-astra", "gpt-6-luna", "gpt-6.1-sol"])


if __name__ == "__main__":
    unittest.main()
