"""Offline regression for real acceptance failures; contains synthetic data only."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import yaml
from cpa_probe.plan import ImportPlan, SectionPlan
from cpa_probe.writeback import mark_new_sections, validate_model_preview, rebuild_config_full


class ModelWriteEvidence(unittest.TestCase):
    def fixture(self, old, new, *, usable=False, verified=(), source="prior"):
        cfg = {"codex-api-key": [{"api-key": "fixture-key", "base-url":
               "https://unit.example/v1", "models": [{"name": m} for m in old],
               "priority": 10}]}
        sp = SectionPlan("codex-api-key", "https://unit.example/v1", "fixture-key",
                         models=list(new), model_source=source, probe_usable=usable,
                         verified_models=list(verified))
        return cfg, sp, ImportPlan("unit.example", "***", sections={sp.section: sp})

    def test_five_live_failure_shapes_preserve_original_models(self):
        cases = [(["a", "b", "c", "d", "e"], ["a"], False, [])] * 3
        cases += [(["a", "b", "c"], ["c", "x", "y", "z"], True, ["c", "x"]),
                  (["a"], ["x"], False, [])]
        for old, new, usable, verified in cases:
            with self.subTest(usable=usable, removed=len(set(old)-set(new))):
                cfg, sp, p = self.fixture(old, new, usable=usable, verified=verified)
                preview = copy.deepcopy(cfg)
                preview[sp.section][0]["models"] = [{"name": m} for m in new]
                self.assertFalse(validate_model_preview(yaml.safe_dump(cfg),
                                 yaml.safe_dump(preview), [p])[0])
                self.assertEqual(mark_new_sections(cfg, [p]), 1)
                self.assertEqual(sp.models, old)
                self.assertFalse(sp.writable)
                self.assertEqual(mark_new_sections(cfg, [p]), 1)
                text, _ = rebuild_config_full(cfg, {(sp.base_url, sp.api_key): p},
                             yaml.safe_dump(cfg).splitlines(keepends=True))
                self.assertEqual(yaml.safe_load(text), cfg)

    def test_verified_addition_is_allowed_and_untested_old_models_retained(self):
        cfg, sp, p = self.fixture(["old"], ["old", "new"], usable=True,
                                  verified=["new"], source="probed")
        self.assertEqual(mark_new_sections(cfg, [p]), 0)
        preview = copy.deepcopy(cfg)
        preview[sp.section][0]["models"].append({"name": "new"})
        self.assertTrue(validate_model_preview(yaml.safe_dump(cfg),
                                             yaml.safe_dump(preview), [p])[0])

    def test_successful_section_and_manual_labels_do_not_prove_models(self):
        for source in ("manual", "probed", "catalog"):
            cfg, sp, p = self.fixture([], ["guess"], usable=True, source=source)
            self.assertEqual(mark_new_sections(cfg, [p]), 1)

    def test_cached_preview_without_evidence_is_refused(self):
        cfg, sp, p = self.fixture(["old"], ["new"])
        preview = copy.deepcopy(cfg)
        preview[sp.section][0]["models"] = [{"name": "new"}]
        for plans in ([], [p]):
            self.assertFalse(validate_model_preview(yaml.safe_dump(cfg),
                             yaml.safe_dump(preview), plans)[0])

    def test_bulk_priority_and_global_tuning_do_not_require_model_probes(self):
        cfg, sp, p = self.fixture(["old"], ["old"])
        preview = copy.deepcopy(cfg)
        preview[sp.section][0]["priority"] = 224
        preview["request-retry"] = 1
        self.assertTrue(validate_model_preview(yaml.safe_dump(cfg),
                         yaml.safe_dump(preview), [])[0])

    def test_existing_enable_disable_and_priority_only_remain_writable(self):
        for disabled in (True, False):
            cfg, sp, p = self.fixture(["old"], ["old"])
            cfg[sp.section][0]["disabled"] = not disabled
            preview = copy.deepcopy(cfg)
            preview[sp.section][0].update(disabled=disabled, priority=224)
            self.assertEqual(mark_new_sections(cfg, [p]), 0)
            self.assertTrue(sp.writable)
            self.assertTrue(validate_model_preview(yaml.safe_dump(cfg),
                             yaml.safe_dump(preview), [])[0])

    def test_new_compat_key_needs_own_evidence(self):
        cfg = {"openai-compatibility": [{"name": "fixture", "base-url":
               "https://unit.example/v1", "api-key-entries": [{"api-key": "old"}],
               "models": [{"name": "model"}]}]}
        preview = copy.deepcopy(cfg)
        preview["openai-compatibility"][0]["api-key-entries"].append({"api-key": "new"})
        self.assertFalse(validate_model_preview(yaml.safe_dump(cfg),
                         yaml.safe_dump(preview), [])[0])

    def test_full_rebuild_checks_new_keys_inherited_provider_models(self):
        for verified in (["a"], ["a", "b", "c"]):
            with self.subTest(verified=verified):
                cfg = {"openai-compatibility": [{"name": "fixture", "base-url":
                       "https://unit.example/v1", "api-key-entries": [{"api-key": "old"}],
                       "models": [{"name": m} for m in ("a", "b", "c")]}]}
                old = SectionPlan("openai-compatibility", "https://unit.example/v1",
                                  "old", provider_name="fixture", models=["a", "b", "c"],
                                  model_source="prior")
                new = SectionPlan("openai-compatibility", "https://unit.example/v1",
                                  "new", provider_name="fixture", models=["a"],
                                  model_source="probed", probe_usable=True,
                                  verified_models=verified)
                plans = [ImportPlan("unit.example", "***", sections={sp.section: sp})
                         for sp in (old, new)]
                self.assertEqual(mark_new_sections(cfg, plans), 0)
                self.assertTrue(old.writable)
                self.assertTrue(new.writable)
                text, _ = rebuild_config_full(cfg,
                    {(sp.base_url, sp.api_key): p for sp, p in zip((old, new), plans)},
                    yaml.safe_dump(cfg).splitlines(keepends=True))
                rows = yaml.safe_load(text)["openai-compatibility"]
                by_key = {k["api-key"]: r for r in rows for k in r["api-key-entries"]}
                self.assertEqual({m["name"] for m in by_key["old"]["models"]}, {"a", "b", "c"})
                self.assertEqual({m["name"] for m in by_key["new"]["models"]}, {"a"})
                self.assertEqual(len({r["name"] for r in rows}), len(rows))
                self.assertEqual(len({r.get("priority", 0) for r in rows}), 1)
                self.assertTrue(validate_model_preview(yaml.safe_dump(cfg), text, plans)[0])

    def test_shared_union_requires_evidence_for_every_key(self):
        for existing, retained, complete in ((False, False, False),
                (True, False, False), (True, True, False), (True, True, True),
                (True, False, True), (False, False, True)):
            with self.subTest(existing=existing, retained=retained, complete=complete):
                cfg = {"openai-compatibility": []}
                if existing:
                    cfg["openai-compatibility"] = [{"name": "fixture", "base-url":
                        "https://unit.example/v1", "api-key-entries":
                        [{"api-key": k} for k in (["one", "two", "retained"]
                                                  if retained else ["one", "two"])],
                        "models": [{"name": "base"}]}]
                sections = [SectionPlan("openai-compatibility", "https://unit.example/v1",
                            key, provider_name="fixture", models=(["base"] if existing else []) + [model],
                            model_source="probed", probe_usable=True,
                            verified_models=["a", "b"] if complete else [model])
                            for key, model in (("one", "a"), ("two", "b"))]
                plans = [ImportPlan("unit.example", "***", sections={sp.section: sp})
                         for sp in sections]
                self.assertEqual(mark_new_sections(cfg, plans), 0)
                text, _ = rebuild_config_full(cfg,
                    {(sp.base_url, sp.api_key): p for sp, p in zip(sections, plans)},
                    yaml.safe_dump(cfg).splitlines(keepends=True))
                self.assertTrue(validate_model_preview(yaml.safe_dump(cfg), text, plans)[0])
                rendered = yaml.safe_load(text)
                rows = rendered["openai-compatibility"]
                by_key = {k["api-key"]: r for r in rows for k in r["api-key-entries"]}
                for sp in sections:
                    self.assertEqual({m["name"] for m in by_key[sp.api_key]["models"]}, set(sp.models))
                if retained:
                    self.assertEqual(by_key["retained"]["models"], [{"name": "base"}])
                if not complete:
                    by_key["one"]["models"].append({"name": "b"})
                    self.assertFalse(validate_model_preview(yaml.safe_dump(cfg),
                                     yaml.safe_dump(rendered), plans)[0])


if __name__ == "__main__":
    unittest.main()
