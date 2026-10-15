"""Regression coverage for preserving configured facts without new evidence."""
from copy import deepcopy
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cpa_probe as cp
from cpa_probe import cpa_source_probe, model_catalog, probe_policy
from cpa_probe.pipeline import Attempt, CandidateResult, SectionVerdict
from cpa_probe.writeback import merge_entry_headers


class ConfigPreservationTests(unittest.TestCase):
    section = "codex-api-key"
    # Same product line, same generation: the "highest generation per line"
    # rule keeps all of these, so a dropped name means evidence was lost
    # rather than a lower tier being pruned.
    configured_models = ["gpt-6-astra", "gpt-6-sol", "gpt-6-terra"]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {
            "IMPORTER_PROBE_MODE": "passive",
            "IMPORTER_PROBE_POLICY_FILE": str(Path(temp.name) / "absent.json"),
            "IMPORTER_PROBE_LEDGER": str(Path(temp.name) / "ledger.sqlite3"),
            "IMPORTER_BACKUP_DIR": temp.name,
        })
        env.start()
        self.addCleanup(env.stop)
        probe_policy.reset_policy_cache()
        self.addCleanup(probe_policy.reset_policy_cache)
        for guard in (
            patch.object(cpa_source_probe, "cached_identity",
                         return_value=cpa_source_probe.CpaIdentity()),
            patch.object(model_catalog, "remote_names", return_value=([], "fixture")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.config = {self.section: [{
            "api-key": "synthetic-key",
            "base-url": "https://fixture.invalid/v1",
            "priority": 3,
            "headers": {"User-Agent": "fixture-client/1", "originator": "fixture-origin"},
            "models": [{"name": name, "alias": ""} for name in self.configured_models],
        }]}
        self.row = cp.parse_lines(
            "https://fixture.invalid,synthetic-key", allow_private=True).valid[0]

    def make_plan(self, *, policy_code="passive", models=(), usable=False, force=None):
        verdict = SectionVerdict(self.section)
        verdict.base_url = self.row.base_for(self.section)
        verdict.models = list(models)
        verdict.catalog = list(models)
        verdict.attempts = [Attempt(
            section=self.section, model=model, combo="baseline", status="200",
            category="ok", action="accept", elapsed_ms=1, resp_model=model,
            response_valid=True, successful_base_url=verdict.base_url)
            for model in models]
        verdict.usable = usable
        verdict.category = "未实测" if policy_code else "失败"
        verdict.probe_policy_code = policy_code
        verdict.probe_policy_reason = "fixture policy stop" if policy_code else ""
        result = CandidateResult(row=self.row)
        result.sections = {self.section: verdict}
        before = deepcopy(self.config)
        plan = cp.build_plan(
            self.row, result, self.config, bands={},
            seen=cp.existing_fingerprints(self.config),
            probation=True, force=force, rebuild=True)
        self.assertEqual(before, self.config, "planning must not mutate the input config")
        return plan.sections[self.section]

    def test_no_evidence_this_round_does_not_add_market_models(self):
        """没有本轮证据时，不许把「市面最新」塞进已有条目的模型清单。

        2026-10-04 复现：给一个**已有**站点追加新 Key 时，这一段本轮根本没探
        （死路/未实测），清单落到 prior（原 config.yaml 的值），随后
        `topup_to_market_top` 仍按远程名录补了几个从没验证过的名字进去。
        后果是「只是加一把 Key」却顺带改写了那条已在工作的条目，写进去的
        是工具猜的名字 —— 正是 tests/test_edges.py 的「现有 models 未被改」
        断言要挡的事。它在断网容器里不失败，只因为名录拉不到。

        每个新增模型都有证据时才补齐（见 test_probed_models_still_top_up_to_market）。
        """
        market = ["gpt-6-astra", "gpt-6-sol", "gpt-6-terra", "gpt-7-nova"]
        with patch.object(model_catalog, "remote_names", return_value=(market, "fixture")):
            section = self.make_plan()
        self.assertEqual(self.configured_models, section.models)
        self.assertEqual("prior", section.model_source)
        self.assertNotIn("gpt-7-nova", section.models)

    def test_probed_models_still_top_up_to_market(self):
        """同代变体完整展示，但每个新增写入项必须有独立成功证据。"""
        market = self.configured_models + ["gpt-6-luna"]
        with patch.object(model_catalog, "remote_names", return_value=(market, "fixture")):
            section = self.make_plan(models=[self.configured_models[0]], usable=True,
                                     policy_code="")
        self.assertIn("gpt-6-luna", section.highest_models)
        self.assertNotIn("gpt-6-luna", section.models)
        self.assertEqual("probed", section.model_source)
        with patch.object(model_catalog, "remote_names", return_value=(market, "fixture")):
            verified = self.make_plan(models=market, usable=True, policy_code="")
        self.assertEqual(set(market), set(verified.models))
        self.assertIn("gpt-6-luna", verified.verified_models)

    def test_passive_keeps_all_configured_models(self):
        section = self.make_plan()
        self.assertEqual(self.configured_models, section.models)
        self.assertEqual("prior", section.model_source)

    def test_policy_preservation_is_not_a_writable_probe_result(self):
        from cpa_probe.plan import ImportPlan
        from cpa_probe.writeback import rebuild_config_full
        import yaml

        raw = "# operator configuration\n" + yaml.safe_dump(self.config, sort_keys=False)
        for code in ("passive", "policy_wait_timeout", "provider_stopped",
                     "provider_job_stopped", "budget_exhausted"):
            with self.subTest(code=code):
                section = self.make_plan(policy_code=code)
                self.assertEqual("prior", section.model_source)
                self.assertEqual(self.configured_models, section.models)
                self.assertFalse(section.writable)
                self.assertFalse(section.recommended)
                self.assertIn("fixture policy stop", section.skip_reason)
                plan = ImportPlan("fixture.invalid", "***",
                                  sections={self.section: section})
                self.assertFalse(plan.any_writable)
                plans = {(section.base_url, section.api_key): plan}
                output, _ = rebuild_config_full(
                    self.config, plans, raw.splitlines(keepends=True))
                self.assertEqual(raw, output)

    def test_partial_probe_then_stop_is_not_writable(self):
        section = self.make_plan(policy_code="provider_stopped",
                                 models=[self.configured_models[0]], usable=True)
        self.assertFalse(section.writable)
        self.assertFalse(section.recommended)
        self.assertEqual(set(self.configured_models), set(section.models))

    def test_completed_probe_retains_write_eligibility(self):
        section = self.make_plan(policy_code="", models=self.configured_models,
                                 usable=True)
        self.assertEqual("probed", section.model_source)
        self.assertTrue(section.writable)

    def test_partial_success_then_policy_stop_keeps_untested_models(self):
        """One model verified then the policy stops: the rest were never tried.

        Treating "not reached" as "tested and rejected" deletes facts an
        earlier probe established.
        """
        section = self.make_plan(policy_code="budget_exhausted",
                                 models=[self.configured_models[0]], usable=True)
        self.assertEqual(set(self.configured_models), set(section.models))
        self.assertTrue(any("\u7b56\u7565\u6253\u65ad" in w for w in section.warnings),
                        section.warnings)

    def test_interruption_preserves_an_untested_older_generation(self):
        old_name = "gpt-5.6-terra"
        self.config[self.section][0]["models"].append({"name": old_name, "alias": ""})
        section = self.make_plan(policy_code="budget_exhausted",
                                 models=[self.configured_models[0]], usable=True)
        self.assertEqual(self.configured_models + [old_name], section.models)

    def test_uninterrupted_selection_keeps_the_existing_generation_rule(self):
        self.config[self.section][0]["models"].append(
            {"name": "gpt-5.6-terra", "alias": ""})
        section = self.make_plan(policy_code="", models=self.configured_models, usable=True)
        self.assertNotIn("gpt-5.6-terra", section.models)

    def test_interruption_can_append_a_new_verified_model(self):
        section = self.make_plan(policy_code="minimum_interval",
                                 models=["gpt-6-nova"], usable=True)
        self.assertEqual(self.configured_models + ["gpt-6-nova"], section.models)

    def test_failed_probe_without_policy_code_keeps_configured_models(self):
        """A dead end with no catalog read learns nothing, so the old list stands."""
        section = self.make_plan(policy_code="")
        self.assertEqual(set(self.configured_models), set(section.models))

    def test_passive_headers_do_not_replace_operator_values(self):
        section = self.make_plan()
        old = self.config[self.section][0]["headers"]
        self.assertEqual(old, merge_entry_headers(old, section.headers))

    def test_failed_probe_headers_do_not_replace_operator_values(self):
        section = self.make_plan(policy_code="")
        old = self.config[self.section][0]["headers"]
        self.assertEqual(old, merge_entry_headers(old, section.headers))

    def test_passive_entry_without_headers_does_not_get_a_guessed_profile(self):
        del self.config[self.section][0]["headers"]
        self.assertEqual({}, self.make_plan().headers)

    def test_explicit_empty_headers_stay_empty_without_measurement(self):
        self.config[self.section][0]["headers"] = {}
        self.assertEqual({}, self.make_plan().headers)

    def test_explicit_model_selection_still_replaces_old_selection(self):
        chosen = [self.configured_models[0]]
        section = self.make_plan(force={self.section: chosen})
        self.assertEqual(chosen, section.models)
        self.assertEqual("manual", section.model_source)




class EmptyHeadersRebuildTests(unittest.TestCase):
    def test_full_rebuild_preserves_empty_vs_absent_headers_on_repeated_write(self):
        import yaml
        from cpa_probe import writeback as wb
        from cpa_probe.plan import ImportPlan, SectionPlan

        for section in ("openai-compatibility", "codex-api-key"):
            for explicit_empty in (False, True):
                with self.subTest(section=section, explicit_empty=explicit_empty):
                    entry = {"base-url": "https://fixture.invalid/v1",
                             "models": [{"name": "fixture-model", "alias": ""}]}
                    if section == "openai-compatibility":
                        entry.update(name="fixture-provider", **{
                            "api-key-entries": [{"api-key": "synthetic-key"}]})
                    else:
                        entry["api-key"] = "synthetic-key"
                    if explicit_empty:
                        entry["headers"] = {}
                    raw = yaml.safe_dump({section: [entry]}, sort_keys=False)
                    sp = SectionPlan(section, entry["base-url"], "synthetic-key",
                                     models=["fixture-model"], model_source="probed",
                                     probe_usable=True, verified_models=["fixture-model"])
                    plans = {(entry["base-url"], "synthetic-key"):
                             ImportPlan("fixture.invalid", "***", sections={section: sp})}
                    for iteration in range(2):
                        raw, _ = wb.rebuild_config_full(
                            yaml.safe_load(raw), plans, raw.splitlines(keepends=True))
                        self.assertTrue(wb.validate(raw)[0])
                        actual = yaml.safe_load(raw)[section][0]
                        self.assertEqual(explicit_empty, "headers" in actual,
                                         f"iteration={iteration}")
                        if explicit_empty:
                            self.assertEqual({}, actual["headers"])


class CompatAppendKeyTests(unittest.TestCase):
    """Adding a key to an existing provider must still land the planned models.

    The append-key branch only rendered the key insert, so a plan whose models
    were not already registered wrote a usable-looking YAML with the new model
    silently missing.
    """

    raw = ("openai-compatibility:\n"
           "  - name: fixture-provider\n"
           "    base-url: \"https://fixture.invalid/v1\"\n"
           "    api-key-entries:\n"
           "      - api-key: \"key-one\"\n"
           "    models:\n"
           "      - name: \"kept-model\"\n"
           "        alias: \"\"\n")

    def section_plan(self, key, models):
        from cpa_probe.plan import ImportPlan, SectionPlan
        sp = SectionPlan("openai-compatibility", "https://fixture.invalid/v1", key,
                         models=list(models), model_source="probed", probe_usable=True,
                         verified_models=list(dict.fromkeys(["kept-model", *models])))
        return ImportPlan("fixture.invalid", "***", sections={sp.section: sp}), sp

    def test_new_key_requires_evidence_for_inherited_provider_models(self):
        import yaml
        from cpa_probe import writeback as wb
        plan, sp = self.section_plan("key-two", ["new-model"])
        sp.verified_models = ["new-model"]
        text = wb.apply_diffs(self.raw, wb.build_diffs(self.raw, [plan]))
        self.assertTrue(sp.writable)
        rows = yaml.safe_load(text)["openai-compatibility"]
        by_key = {k["api-key"]: r for r in rows for k in r["api-key-entries"]}
        self.assertEqual([m["name"] for m in by_key["key-two"]["models"]], ["new-model"])
        self.assertEqual(by_key["key-one"]["models"], yaml.safe_load(self.raw)["openai-compatibility"][0]["models"])
        self.assertTrue(wb.validate_model_preview(self.raw, text, [plan])[0])

    def test_new_key_requires_usable_probe_evidence(self):
        import yaml
        from cpa_probe import writeback as wb
        plan, sp = self.section_plan("key-two", ["kept-model", "new-model"])
        sp.probe_usable = None
        text = wb.apply_diffs(self.raw, wb.build_diffs(self.raw, [plan]))
        self.assertEqual(yaml.safe_load(self.raw), yaml.safe_load(text))

    def rendered(self, key, models):
        import yaml
        from cpa_probe import writeback as wb
        plan, sp = self.section_plan(key, models)
        diffs = wb.build_diffs(self.raw, [plan])
        text = wb.apply_diffs(self.raw, diffs)
        valid, why = wb.validate(text)
        self.assertTrue(valid, why)
        config = yaml.safe_load(text)
        rows = config["openai-compatibility"]
        provider = next(r for r in rows if any(k["api-key"] == key for k in r["api-key-entries"]))
        keys = [entry.get("api-key") for r in rows for entry in r["api-key-entries"]]
        names = [model.get("name") for model in provider["models"] or []]
        return text, keys, names

    def test_appending_a_key_registers_the_planned_models(self):
        _text, keys, names = self.rendered("key-two", ["kept-model", "new-model"])
        # Both the registered key and the new one must survive, and the planned
        # model must actually land. Order is not part of the contract: a
        # structural rebuild may emit the planned key first.
        self.assertEqual({"key-one", "key-two"}, set(keys))
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({"kept-model", "new-model"}, set(names))
        self.assertEqual(len(names), len(set(names)))

    def test_new_key_with_only_new_models_preserves_old_provider_models(self):
        _text, keys, names = self.rendered("key-two", ["new-model"])
        self.assertEqual({"key-one", "key-two"}, set(keys))
        self.assertEqual(["new-model"], names)

    def test_append_preserves_provider_without_retained_key_model_evidence(self):
        import yaml
        from cpa_probe import writeback as wb
        for models in (["kept-model", "new-model"], ["new-model"]):
            with self.subTest(models=models):
                plan, sp = self.section_plan("key-two", models)
                text = wb.apply_diffs(self.raw, wb.build_diffs(self.raw, [plan]))
                self.assertTrue(sp.writable)
                rows = yaml.safe_load(text)["openai-compatibility"]
                old = next(r for r in rows if r["name"] == "fixture-provider")
                self.assertEqual(old["models"], yaml.safe_load(self.raw)["openai-compatibility"][0]["models"])
                self.assertEqual(old["api-key-entries"], [{"api-key": "key-one"}])
                self.assertTrue(wb.validate_model_preview(self.raw, text, [plan])[0])

    def test_existing_key_disambiguates_providers_on_the_same_base(self):
        import yaml
        from cpa_probe import writeback as wb
        config = yaml.safe_load(self.raw)
        other = deepcopy(config["openai-compatibility"][0])
        other["name"] = "other-provider"
        other["api-key-entries"] = [{"api-key": "other-key"}]
        config["openai-compatibility"].append(other)
        _plan, section = self.section_plan("key-one", ["kept-model"])
        self.assertEqual("fixture-provider", wb._original_entry(config, section)["name"])

    def test_new_key_rejects_ambiguous_providers_on_the_same_base(self):
        import yaml
        from cpa_probe import writeback as wb
        config = yaml.safe_load(self.raw)
        other = deepcopy(config["openai-compatibility"][0])
        other["name"] = "other-provider"
        other["api-key-entries"] = [{"api-key": "other-key"}]
        config["openai-compatibility"].append(other)
        _plan, section = self.section_plan("key-two", ["new-model"])
        with self.assertRaises(ValueError):
            wb._original_entry(config, section)

    def test_appending_the_same_key_twice_is_idempotent(self):
        import yaml
        from cpa_probe import writeback as wb
        first, keys, names = self.rendered("key-two", ["kept-model", "new-model"])
        plan, _sp = self.section_plan("key-two", ["kept-model", "new-model"])
        second = wb.apply_diffs(first, wb.build_diffs(first, [plan]))
        self.assertEqual(yaml.safe_load(first), yaml.safe_load(second))
        # A stale pre-split source name still resolves the credential's current
        # provider by exact key, without duplicating it or growing hash suffixes.
        plan, sp = self.section_plan("key-two", ["kept-model", "new-model"])
        sp.provider_name = "fixture-provider"
        third = wb.apply_diffs(second, wb.build_diffs(second, [plan]))
        self.assertEqual(yaml.safe_load(second), yaml.safe_load(third))

class SameHostDifferentPathTests(unittest.TestCase):
    """Two upstreams on one host but different paths are different entries.

    The carry tables keyed on host alone, so the second entry's weight, proxy,
    headers and prefix overwrote the first one's. CPA routes by the full
    base-url, so the surviving entry silently got the other one's parameters.
    """

    section = "codex-api-key"
    config = {
        "codex-api-key": [
            {"api-key": "shared-key", "base-url": "https://fixture.invalid/alpha/v1",
             "priority": 3, "weight": 11, "prefix": "ALPHA",
             "proxy-url": "http://proxy.invalid:1081",
             "headers": {"x-fixture-lane": "alpha"},
             "models": [{"name": "gpt-6-astra", "alias": ""}]},
            {"api-key": "shared-key", "base-url": "https://fixture.invalid/beta/v1",
             "priority": 3, "weight": 22, "prefix": "BETA",
             "proxy-url": "http://proxy.invalid:1082",
             "headers": {"x-fixture-lane": "beta"},
             "models": [{"name": "gpt-6-sol", "alias": ""}]},
        ]
    }

    def lane(self, path):
        return "https://fixture.invalid/%s/v1" % path

    def carried(self, path):
        """Run the real carry path: build the tables, then apply to a plan."""
        from cpa_probe.batch import CarryTables
        from cpa_probe.plan import SectionPlan
        sp = SectionPlan(self.section, self.lane(path), "shared-key",
                         models=["gpt-6-astra"], model_source="probed")
        CarryTables(self.config).apply(sp, "shared-key")
        return sp

    def test_weight_does_not_cross_paths(self):
        self.assertEqual(11, self.carried("alpha").weight)
        self.assertEqual(22, self.carried("beta").weight)

    def test_proxy_does_not_cross_paths(self):
        self.assertEqual("http://proxy.invalid:1081", self.carried("alpha").proxy_url)
        self.assertEqual("http://proxy.invalid:1082", self.carried("beta").proxy_url)

    def test_headers_do_not_cross_paths(self):
        self.assertEqual({"x-fixture-lane": "alpha"}, self.carried("alpha").headers)
        self.assertEqual({"x-fixture-lane": "beta"}, self.carried("beta").headers)

    def test_prefix_does_not_cross_paths(self):
        self.assertEqual("ALPHA", self.carried("alpha").prefix)
        self.assertEqual("BETA", self.carried("beta").prefix)


class AliasContextTests(unittest.TestCase):
    """Two aliases of one model keep their own context limits.

    The prior-context table was keyed on the model name only, so restoring the
    aliases wrote the last value over both of them. Without a fresh capacity
    measurement, neither limit has new evidence to justify the change.
    """

    section = "codex-api-key"
    config = {
        "codex-api-key": [{
            "api-key": "alias-key", "base-url": "https://fixture.invalid/v1",
            "priority": 3,
            "models": [
                {"name": "gpt-6-astra", "alias": "small", "max-context-length": 100},
                {"name": "gpt-6-astra", "alias": "large", "max-context-length": 200},
            ],
        }]
    }

    def rebuild_models(self, config=None, measured=0):
        import yaml
        from cpa_probe import writeback as wb
        from cpa_probe.batch import CarryTables
        from cpa_probe.plan import ImportPlan, SectionPlan
        config = deepcopy(self.config if config is None else config)
        before = deepcopy(config)
        raw = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        names = list(dict.fromkeys(m["name"] for m in config[self.section][0]["models"]))
        sp = SectionPlan(self.section, "https://fixture.invalid/v1", "alias-key",
                         models=names, model_source="probed")
        if measured:
            sp.max_context_length = measured
            sp.context_model = "gpt-6-astra"
        CarryTables(config).apply(sp, "alias-key")
        plans = {(sp.base_url, sp.api_key): ImportPlan(
            "fixture.invalid", "***", sections={sp.section: sp})}
        text, _ = wb.rebuild_config_full(config, plans, raw.splitlines(keepends=True))
        valid, why = wb.validate(text)
        self.assertTrue(valid, why)
        self.assertEqual(before, config)
        return yaml.safe_load(text)[self.section][0]["models"]

    def test_rebuild_keeps_both_alias_limits_without_new_evidence(self):
        limits = {(m.get("name"), m.get("alias")): m.get("max-context-length")
                  for m in self.rebuild_models()}
        self.assertEqual({("gpt-6-astra", "small"): 100,
                          ("gpt-6-astra", "large"): 200}, limits)

    def test_alias_without_limit_does_not_inherit_another_alias_limit(self):
        config = deepcopy(self.config)
        del config[self.section][0]["models"][0]["max-context-length"]
        models = self.rebuild_models(config)
        by_alias = {m["alias"]: m for m in models}
        self.assertNotIn("max-context-length", by_alias["small"])
        self.assertEqual(200, by_alias["large"]["max-context-length"])

    def test_fresh_measurement_updates_only_the_measured_model(self):
        config = deepcopy(self.config)
        config[self.section][0]["models"].append(
            {"name": "gpt-6-sol", "alias": "other", "max-context-length": 700})
        limits = {(m["name"], m["alias"]): m.get("max-context-length")
                  for m in self.rebuild_models(config, measured=400)}
        self.assertEqual({("gpt-6-astra", "small"): 400,
                          ("gpt-6-astra", "large"): 400,
                          ("gpt-6-sol", "other"): 700}, limits)


class DefaultPriorityTests(unittest.TestCase):
    section = "codex-api-key"

    def facts(self, rows):
        from cpa_probe.tuning import tier_facts
        return tier_facts({self.section: rows})[self.section]

    def entry(self, key, **fields):
        return {"api-key": key, "base-url": "https://fixture.invalid/v1",
                "models": [{"name": "gpt-6-astra"}], **fields}

    def test_missing_priority_counts_as_zero(self):
        facts = self.facts([self.entry("first")])
        self.assertEqual(0, facts.top_priority)
        self.assertEqual(1, facts.credentials)

    def test_implicit_zero_ranks_above_negative_priority(self):
        facts = self.facts([self.entry("first"), self.entry("second", priority=-2)])
        self.assertEqual(0, facts.top_priority)
        self.assertEqual(1, facts.credentials)

    def test_implicit_and_explicit_zero_share_the_top_pool(self):
        facts = self.facts([self.entry("first"), self.entry("second", priority=0)])
        self.assertEqual(0, facts.top_priority)
        self.assertEqual(2, facts.credentials)
        self.assertEqual(2, facts.longest_same_host_run)

    def test_disabled_implicit_priority_is_not_counted(self):
        # Native sections are disabled with excluded-models, not the
        # compatibility-provider-only disabled flag.
        facts = self.facts([self.entry("first", **{"excluded-models": ["*"]}),
                            self.entry("second", priority=-2)])
        self.assertEqual(-2, facts.top_priority)
        self.assertEqual(1, facts.credentials)

    def test_invalid_explicit_priority_is_not_silently_defaulted(self):
        facts = self.facts([self.entry("first", priority="invalid"),
                            self.entry("second", priority=-2)])
        self.assertEqual(-2, facts.top_priority)
        self.assertEqual(1, facts.credentials)

    def test_disabled_compat_provider_is_not_counted(self):
        from cpa_probe.tuning import tier_facts
        section = "openai-compatibility"
        config = {section: [
            {"name": "off", "base-url": "https://fixture.invalid/off",
             "disabled": True, "api-key-entries": [{"api-key": "first"}],
             "models": [{"name": "gpt-6-astra"}]},
            {"name": "on", "base-url": "https://fixture.invalid/on",
             "priority": -2, "api-key-entries": [{"api-key": "second"}],
             "models": [{"name": "gpt-6-astra"}]},
        ]}
        facts = tier_facts(config)[section]
        self.assertEqual(-2, facts.top_priority)
        self.assertEqual(1, facts.credentials)

    def test_empty_pool_has_no_credentials(self):
        self.assertEqual(0, self.facts([]).credentials)


class FinalSelectedModelGateTests(unittest.TestCase):
    def plan(self, models):
        from cpa_probe.plan import SectionPlan, ImportPlan
        sp = SectionPlan("openai-compatibility", "https://fixture.invalid/v1",
                         "fixture-key", models=models, model_source="probed")
        return ImportPlan("fixture.invalid", "***", sections={sp.section: sp})

    def test_selected_empty_section_is_rejected_even_when_renderer_skips_it(self):
        from server import _validate_final
        ok, why = _validate_final("{}", [self.plan([])])
        self.assertFalse(ok)
        self.assertIn("没有任何模型", why)

    def test_populated_selection_passes(self):
        from server import _validate_final
        ok, why = _validate_final("{}", [self.plan(["gpt-6-astra"])])
        self.assertTrue(ok, why)

    def test_generator_selection_is_checked(self):
        from server import _validate_final
        ok, _ = _validate_final("{}", (p for p in [self.plan([])]))
        self.assertFalse(ok)

    def test_unselected_legacy_entry_is_preserved(self):
        from server import _validate_final
        ok, why = _validate_final("codex-api-key:\n- base-url: https://fixture.invalid/v1\n  api-key: fixture-key\n")
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main()
