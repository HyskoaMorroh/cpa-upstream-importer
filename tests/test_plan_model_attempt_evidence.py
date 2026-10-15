"""Credential-local model proof must survive shape and catalog reuse."""
import unittest
from types import SimpleNamespace

from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import Attempt, SectionVerdict
from cpa_probe.plan import _current_verified_models


class CurrentModelEvidence(unittest.TestCase):
    def setUp(self):
        self.row = ParsedRow(1, "", "https://unit.example", "second-key")
        self.result = SimpleNamespace(row=self.row)
        self.base = "https://unit.example/v1"
        self.verdict = SectionVerdict("codex-api-key", self.base, True,
                                      ["gpt-5", "gpt-5-mini"])

    def attempt(self, model="gpt-5", **overrides):
        args = dict(section="codex-api-key", model=model, combo="baseline",
                    status="200", category="ok", action="accept", elapsed_ms=1,
                    resp_model=model, response_valid=True,
                    successful_base_url=self.base)
        args.update(overrides)
        return Attempt(**args)

    def proof(self):
        return _current_verified_models(self.row, self.result, self.verdict, self.base)

    def test_shape_list_does_not_prove_unprobed_models(self):
        self.verdict.attempts = [self.attempt()]
        self.assertEqual(self.proof(), ["gpt-5"])

    def test_every_successful_model_remains_covered(self):
        self.verdict.attempts = [self.attempt(m) for m in self.verdict.models]
        self.assertEqual(self.proof(), self.verdict.models)

    def test_supported_response_names_remain_compatible(self):
        for actual in ("gpt-5", "gpt-5-2025-08-07"):
            with self.subTest(actual=actual):
                self.verdict.attempts = [self.attempt(resp_model=actual,
                    successful_base_url=self.base + "/")]
                self.assertEqual(self.proof(), ["gpt-5"])

    def test_missing_identity_does_not_prove_model_for_writing(self):
        self.verdict.attempts = [self.attempt(resp_model=None)]
        self.assertTrue(self.verdict.attempts[0].ok)
        self.assertEqual(self.proof(), [])

    def test_endpoint_normalization_preserves_declared_path(self):
        self.verdict.attempts = [self.attempt(
            successful_base_url="https://UNIT.EXAMPLE/v1/")]
        self.assertEqual(self.proof(), ["gpt-5"])
        self.verdict.attempts = [self.attempt(
            successful_base_url="https://unit.example")]
        self.assertEqual(self.proof(), [])

    def test_wrong_credential_or_source_cannot_supply_evidence(self):
        self.verdict.attempts = [self.attempt()]
        for bare, key in [(self.row.bare, "first-key"),
                          ("https://elsewhere.example", self.row.api_key)]:
            self.result.row = ParsedRow(1, "", bare, key)
            self.assertEqual(self.proof(), [])

    def test_status_alone_wrong_endpoint_or_substituted_model_are_rejected(self):
        for overrides in [dict(response_valid=False), dict(error_envelope=True),
                          dict(successful_base_url=""),
                          dict(successful_base_url="https://elsewhere.example/v1"),
                          dict(section="openai-compatibility"),
                          dict(resp_model="gpt-5-mini"), dict(status="429")]:
            with self.subTest(overrides=overrides):
                self.verdict.attempts = [self.attempt(**overrides)]
                self.assertEqual(self.proof(), [])


if __name__ == "__main__":
    unittest.main()
