"""Semantic success must not manufacture model identity evidence."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cpa_probe.fingerprint import resp_model
from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import Attempt, CandidateResult, SectionVerdict
from cpa_probe.plan import _current_verified_models
from cpa_probe.writeback import verify_upstream


class ModelIdentityBoundaryTests(unittest.TestCase):
    def evidence(self, actual, section="codex-api-key", model="gpt-6-sol"):
        row = ParsedRow(1, "", "https://fixture.example", "fixture-key")
        base = "https://fixture.example/v1"
        attempt = Attempt(section=section, model=model, combo="baseline",
                          status="200", category="ok", action="accept", elapsed_ms=1,
                          resp_model=actual, response_valid=True,
                          successful_base_url=base)
        verdict = SectionVerdict(section=section, usable=True, models=[model],
                                 successful_base_url=base, attempts=[attempt])
        return row, CandidateResult(row, {section: verdict}), verdict, base

    def test_missing_identity_keeps_semantic_success_without_proving_model(self):
        for actual in (None, ""):
            args = self.evidence(actual)
            self.assertTrue(args[2].attempts[0].ok)
            self.assertEqual(_current_verified_models(*args), [])

    def test_gemini_model_version_is_identity_evidence(self):
        model = "gemini-2.5-pro"
        actual = resp_model(json.dumps({"modelVersion": model}))
        args = self.evidence(actual, "gemini-api-key", model)
        self.assertEqual(_current_verified_models(*args), [model])

    def test_explicit_mismatch_is_not_proven(self):
        self.assertEqual(_current_verified_models(*self.evidence("gpt-6-sol-mini")), [])

    def test_different_endpoint_or_section_does_not_prove_model(self):
        for field, value in (("successful_base_url", "https://fixture.example/other"),
                             ("section", "openai-compatibility")):
            args = self.evidence("gpt-6-sol")
            setattr(args[2].attempts[0], field, value)
            self.assertEqual(_current_verified_models(*args), [])

    def test_gateway_semantic_success_reports_unknown_identity(self):
        body = json.dumps({"candidates": [{"content": {"parts": [{"text": "4"}]},
                                          "finishReason": "STOP"}]})
        scope = {"direct_target": True}
        response = SimpleNamespace(status="200", body=body, policy_code="")
        with patch("cpa_probe.client.send", return_value=response):
            ok, reason = verify_upstream("https://fixture.example", "fixture-key",
                                         "gemini-api-key", "gemini-2.5-pro", scope=scope)
        self.assertTrue(ok, reason)
        self.assertFalse(scope["model_identity_verified"])
        self.assertFalse(scope["target_verified"])
        self.assertIn("未核实模型身份", reason)

    def test_gateway_gemini_identity_is_reported(self):
        body = json.dumps({"modelVersion": "gemini-2.5-pro", "candidates": [
            {"content": {"parts": [{"text": "4"}]}, "finishReason": "STOP"}]})
        scope = {"direct_target": True}
        response = SimpleNamespace(status="200", body=body, policy_code="")
        with patch("cpa_probe.client.send", return_value=response):
            ok, reason = verify_upstream("https://fixture.example", "fixture-key",
                                         "gemini-api-key", "gemini-2.5-pro", scope=scope)
        self.assertTrue(ok, reason)
        self.assertTrue(scope["model_identity_verified"])
        self.assertFalse(scope["target_verified"])


if __name__ == "__main__":
    unittest.main()
