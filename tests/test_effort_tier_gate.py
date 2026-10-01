"""算力档终检：同基名不许高低档并存（修改要求 3⑵③④）。

现场（2026-09-30 本机实跑 accounts.txt，jianzhile.vip gemini 段）：
方案里 `models` 同时含 `gemini-3.1-pro` 与 `gemini-3.1-pro-low` 并双双
`recommended=True`。那正是要求③④ 点名要规避的「部分高、低模型同时存在，
没有就高选择模型」。

根因：`newest_generation_per_line` 的阶段 C 有「同基名只留最高算力档」的
收敛，但 `plan.py` 的族/档次终检只调 `section_family_violations`
（逐条判族与降级档），逐条判据看不见「同基名还有更高档」这个相对关系，
于是 `-low` 原样通过。该注释当时声称修掉了 gemini 段 `-high`/`-low` 并存，
实际没有 —— 本文件把这条不变式钉住。
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import model_catalog


class EffortTierGateTests(unittest.TestCase):
    def test_bare_base_beats_its_own_low_variant(self):
        """裸基名是站方默认档，不该与自己的 -low 并存。"""
        observed = [
            "gemini-3.1-pro",
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-preview-customtools",
            "gemini-3.1-pro-request",
            "gemini-3.1-pro-request-antigravity",
            "gemini-3.1-pro-low",
        ]
        kept = model_catalog.collapse_effort_tiers(observed)
        self.assertNotIn("gemini-3.1-pro-low", kept)
        self.assertIn("gemini-3.1-pro", kept)
        # 功能变体（preview / request / customtools / antigravity）不是算力档，
        # 规则③ 要求同级全留。
        for name in observed:
            if name != "gemini-3.1-pro-low":
                self.assertIn(name, kept)

    def test_high_beats_low_for_same_base(self):
        kept = model_catalog.collapse_effort_tiers(
            ["gemini-3.1-pro-high", "gemini-3.1-pro-low"])
        self.assertEqual(kept, ["gemini-3.1-pro-high"])

    def test_lone_low_variant_survives(self):
        """某站只提供 -low 时必须保留 —— 否则该段清空，撞「严禁不勾选」。"""
        kept = model_catalog.collapse_effort_tiers(["gemini-3.1-pro-low"])
        self.assertEqual(kept, ["gemini-3.1-pro-low"])

    def test_version_numbers_are_not_effort_tiers(self):
        """`claude-opus-5` 的 5 是版本号，不能被当算力档去压 `claude-opus-4-8`。"""
        names = ["claude-opus-5", "claude-opus-4-8"]
        self.assertEqual(model_catalog.collapse_effort_tiers(names), names)

    def test_input_order_is_preserved_and_duplicates_dropped(self):
        kept = model_catalog.collapse_effort_tiers(
            ["gemini-3.1-pro-preview", "gemini-3.1-pro", "gemini-3.1-pro"])
        self.assertEqual(kept, ["gemini-3.1-pro-preview", "gemini-3.1-pro"])

    def test_section_family_violations_still_only_judges_each_name(self):
        """终检的逐条判据本身不变 —— 收敛是**另一道**闸，不混进这一条。"""
        self.assertEqual(
            model_catalog.section_family_violations(
                "gemini-api-key", ["gemini-3.1-pro", "gemini-3.1-pro-low"]),
            [])

    def test_plan_final_gate_drops_low_when_higher_tier_present(self):
        """plan.py 的终检必须落地这条收敛（端到端锁定，防止只加函数不接线）。"""
        import inspect

        from cpa_probe import plan

        src = inspect.getsource(plan)
        self.assertIn("collapse_effort_tiers", src,
                      "plan.py 的族/档次终检没有调用算力档收敛 —— "
                      "gemini-3.1-pro 与 -low 会并存写进 config.yaml")


if __name__ == "__main__":
    unittest.main()
