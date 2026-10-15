"""Automatic proposals preserve history while separating untested candidates."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import Attempt, CandidateResult, SectionVerdict
from cpa_probe.plan import build_plan
from cpa_probe.writeback import model_change_block


class EvidenceProposalTests(unittest.TestCase):
    def build(self, section="openai-compatibility", *, stopped=False,
              existing=True, mismatch=False, complete=False, rebuild=True):
        base = "https://fixture.example/v1"
        row = ParsedRow(1, "", "https://fixture.example", "fixture-key")
        old = ["gpt-5", "gpt-5-mini"]
        tested, untested = "gpt-6-sol", "gpt-6-astra"
        entry = {"base-url": base, "models": [
            {"name": old[0], "alias": "legacy-alias"}, {"name": old[1]}]}
        if section == "openai-compatibility":
            entry.update(name="fixture", **{"api-key-entries": [{"api-key": row.api_key}]})
        else:
            entry["api-key"] = row.api_key
        cfg = {section: [entry]} if existing else {}
        before = copy.deepcopy(cfg)
        attempt = Attempt(section=section, model=tested, combo="baseline",
                          status="200", category="ok", action="accept", elapsed_ms=1,
                          resp_model=tested, response_valid=True,
                          successful_base_url=base)
        verdict = SectionVerdict(section=section, usable=True,
                                 successful_base_url=base,
                                 models=[tested, untested], catalog=[tested, untested],
                                 attempts=[attempt], min_headers={"originator": "fixture"})
        if complete:
            peer = copy.deepcopy(attempt)
            peer.model = peer.resp_model = untested
            verdict.attempts.append(peer)
        if stopped:
            verdict.probe_policy_code = "provider_job_stopped"
            verdict.probe_policy_reason = "fixture stop"
        result = CandidateResult(row, {section: verdict})
        if mismatch:
            result.row = ParsedRow(1, "", row.bare, "other-key")
        ident = SimpleNamespace(claude_cloak_modes=[], claude_fingerprint_profiles=[])
        with patch("cpa_probe.model_catalog.remote_names", return_value=([tested, untested], "")), \
                patch("cpa_probe.cpa_source_probe.cached_identity", return_value=ident), \
                patch("cpa_probe.plan._fallback_headers", return_value={"originator": "fixture"}):
            plan = build_plan(row, result, cfg, rebuild=rebuild)
        self.assertEqual(cfg, before, "planning must preserve alias objects and input")
        return cfg, plan.sections[section], old, tested, untested

    def test_success_preserves_untested_history_and_only_adds_proven_model(self):
        for section in ("codex-api-key", "openai-compatibility"):
            with self.subTest(section=section):
                cfg, sp, old, tested, untested = self.build(section)
                self.assertEqual(sp.models, old + [tested])
                self.assertEqual(sp.verified_models, [tested])
                self.assertEqual(model_change_block(cfg, sp), "")
                self.assertEqual(sp.model_provenance[old[0]], "inferred")
                self.assertIn(untested, sp.highest_models)
                self.assertEqual(sp.model_provenance[untested], "inferred")
                self.assertTrue(any("待逐模型补探" in w for w in sp.warnings))

    def test_new_entry_never_adds_catalog_or_shape_only_model(self):
        cfg, sp, old, tested, untested = self.build(existing=False)
        self.assertEqual(sp.models, [tested])
        self.assertEqual(model_change_block(cfg, sp), "")
        self.assertIn(untested, sp.highest_models)

    def test_other_credentials_evidence_cannot_add_models(self):
        cfg, sp, old, tested, untested = self.build(mismatch=True)
        self.assertEqual(sp.models, old)
        self.assertEqual(sp.verified_models, [])

    def test_provider_stop_remains_blocked_despite_safe_model_proposal(self):
        cfg, sp, old, tested, untested = self.build(stopped=True)
        self.assertEqual(sp.models, old + [tested])
        self.assertEqual(sp.probe_policy_code, "provider_job_stopped")
        self.assertFalse(sp.writable)

    def test_complete_family_rebuild_replaces_only_older_generation(self):
        cfg, sp, old, tested, peer = self.build(complete=True)
        self.assertEqual(sp.models, [tested, peer])
        self.assertEqual(model_change_block(cfg, sp), "")
        self.assertTrue(sp.model_rebuild)

    def test_adding_account_preserves_history_even_with_complete_family(self):
        cfg, sp, old, tested, peer = self.build(complete=True, rebuild=False)
        self.assertEqual(sp.models, old + [tested, peer])
        self.assertFalse(sp.model_rebuild)

    def test_incomplete_family_cannot_remove_history_during_rebuild(self):
        cfg, sp, old, tested, peer = self.build()
        self.assertTrue(set(old).issubset(sp.models))
        self.assertTrue(any("尚未全部验证" in warning for warning in sp.warnings))


if __name__ == "__main__":
    unittest.main()
