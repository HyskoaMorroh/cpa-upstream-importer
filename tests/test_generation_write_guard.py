"""Commit-time checks for the narrow complete-family rebuild exception."""
import copy
import unittest
from types import SimpleNamespace

import yaml

from cpa_probe.model_catalog import policy_lower_models
from cpa_probe.plan import SectionPlan
from cpa_probe.writeback import model_change_block, validate_model_preview


class GenerationWriteGuardTests(unittest.TestCase):
    def fixture(self):
        section = "codex-api-key"
        highest = ["gpt-6-sol", "gpt-6-astra"]
        cfg = {section: [{"api-key": "fixture-key", "base-url": "https://fixture.example/v1",
                          "models": [{"name": "gpt-5", "alias": "legacy"}]}]}
        sp = SectionPlan(section, "https://fixture.example/v1", "fixture-key",
                         models=highest.copy(), highest_models=highest.copy(),
                         verified_models=highest.copy(), probe_usable=True, model_rebuild=True)
        return cfg, sp

    def test_complete_highest_family_allows_strict_old_generation_removal(self):
        cfg, sp = self.fixture()
        self.assertEqual(model_change_block(cfg, sp), "")

    def test_add_mode_never_deletes_even_verified_history(self):
        cfg, sp = self.fixture()
        sp.model_rebuild = False
        sp.verified_models.append("gpt-5")
        self.assertTrue(model_change_block(cfg, sp))

    def test_partial_pending_stopped_or_failed_never_authorizes_deletion(self):
        for field, value in (("verified_models", ["gpt-6-sol"]),
                             ("model_scan_pending", ["gpt-6-astra"]),
                             ("probe_policy_code", "provider_job_stopped"),
                             ("probe_usable", False)):
            cfg, sp = self.fixture()
            setattr(sp, field, value)
            self.assertTrue(model_change_block(cfg, sp), field)

    def test_custom_other_family_and_same_generation_cannot_be_removed(self):
        for name in ("custom-model-1", "claude-opus-5", "gpt-6-luna", "gpt-6-mini"):
            cfg, sp = self.fixture()
            cfg[sp.section][0]["models"].append({"name": name})
            sp.verified_models.append(name)
            self.assertTrue(model_change_block(cfg, sp), name)

    def test_preview_cannot_omit_verified_highest_sibling_or_add_unproven_model(self):
        cfg, sp = self.fixture()
        plans = [SimpleNamespace(sections={sp.section: sp})]
        for names in (["gpt-6-sol"], sp.models + ["gpt-7"]):
            proposed = copy.deepcopy(cfg)
            proposed[sp.section][0]["models"] = [{"name": n} for n in names]
            ok, _ = validate_model_preview(yaml.safe_dump(cfg), yaml.safe_dump(proposed), plans)
            self.assertFalse(ok)

    def test_preview_cannot_borrow_evidence_from_other_path(self):
        cfg, sp = self.fixture()
        proposed = copy.deepcopy(cfg)
        proposed[sp.section][0]["base-url"] += "/other"
        proposed[sp.section][0]["models"] = [{"name": n} for n in sp.models]
        ok, _ = validate_model_preview(yaml.safe_dump(cfg), yaml.safe_dump(proposed),
                                       [SimpleNamespace(sections={sp.section: sp})])
        self.assertFalse(ok)

    def test_policy_preserves_parallel_lines_and_unknown_models(self):
        self.assertEqual(policy_lower_models(["gpt-6-astra", "gpt-6-sol", "custom-5"],
                                             ["gpt-6.1-sol"]), ["gpt-6-sol"])


if __name__ == "__main__":
    unittest.main()
