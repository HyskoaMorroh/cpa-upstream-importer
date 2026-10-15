"""Cache reuse must produce credential-specific evidence for every accepted model."""
import unittest
from unittest.mock import Mock

from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import Attempt, CandidateResult, Prober, ProbePolicyStop, SectionVerdict
from cpa_probe.plan import build_plan


SECTION = "openai-compatibility"
BASE = "https://upstream.example/tenant/v1"


def attempt(model, *, ok=True, identity=False, response_model=None):
    return Attempt(
        section=SECTION, model=model, combo="reuse-verify",
        status="200" if ok else "404", category="ok" if ok else "missing",
        action="", elapsed_ms=1, response_valid=ok,
        resp_model=response_model or model, identity_verified=identity,
        successful_base_url=BASE if ok else "",
    )


class ShapeReuseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.prober = object.__new__(Prober)
        self.prober._base = Mock(return_value=BASE)
        self.prober._profile_kwargs = Mock(return_value={})
        self.prober._check_cancel = Mock()
        # This suite isolates cached-model verification; family coverage has its own suite.
        self.prober._stage2 = Mock()
        self.prober.on_event = Mock()
        self.row = ParsedRow(1, "", "https://upstream.example/tenant", "current-key")
        self.shape = SectionVerdict(
            section=SECTION, usable=True,
            models=["model-a", "model-b", "model-c"],
            identity_verified_models=["model-a", "model-b", "model-c"],
            context_model="model-b", max_context_length=12345,
        )

    def test_every_model_uses_current_credential_and_exact_base(self):
        self.prober._call = Mock(side_effect=lambda *a, **kw: attempt(a[3]))
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        self.assertEqual(v.models, self.shape.models)
        self.assertEqual(len(v.attempts), 3)
        self.assertEqual(v.identity_verified_models, [])
        for call in self.prober._call.call_args_list:
            self.assertEqual(call.args[1:3], (BASE, "current-key"))
        self.assertEqual(v.successful_base_url, BASE)
        self.prober._stage2.assert_called_once_with(self.row, v)

    def test_failed_first_and_middle_models_do_not_hide_later_success(self):
        self.prober._call = Mock(side_effect=[
            attempt("model-a", ok=False), attempt("model-b", ok=False),
            attempt("model-c", identity=True),
        ])
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        self.assertTrue(v.usable)
        self.assertEqual(v.models, ["model-c"])
        self.assertEqual(v.identity_verified_models, ["model-c"])
        self.assertEqual(len(v.attempts), 3)
        self.assertIsNone(v.max_context_length)
        self.assertEqual(v.context_model, "")

    def test_empty_model_cache_never_becomes_usable(self):
        self.shape.models = []
        self.prober._call = Mock()
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        self.assertFalse(v.usable)
        self.prober._call.assert_not_called()

    def test_policy_stop_before_first_attempt_is_not_a_success(self):
        self.prober._call = Mock(side_effect=ProbePolicyStop("provider_job_stopped", "stop"))
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        self.assertFalse(v.usable)
        self.assertEqual(v.models, [])
        self.assertEqual(v.probe_policy_code, "provider_job_stopped")

    def test_policy_stop_retains_only_completed_success(self):
        self.prober._call = Mock(side_effect=[
            attempt("model-a"), ProbePolicyStop("budget_exhausted", "stop"),
        ])
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        self.assertEqual(v.models, ["model-a"])
        self.assertEqual(len(v.attempts), 1)
        self.assertEqual(v.probe_policy_code, "budget_exhausted")
        self.assertEqual(self.prober._call.call_count, 2)
        self.prober._stage2.assert_not_called()

    def test_changed_model_identity_triggers_full_probe(self):
        self.prober._call = Mock(side_effect=[
            attempt("model-a"), attempt("model-b", response_model="other-model"),
        ])
        fresh = SectionVerdict(section=SECTION)
        self.prober._full_probe = Mock(return_value=fresh)
        self.assertIs(self.prober._reuse_shape(self.row, SECTION, self.shape), fresh)
        self.prober._full_probe.assert_called_once_with(self.row, SECTION)

    def test_write_plan_keeps_only_current_credential_successes(self):
        self.shape.models = ["gpt-6-astra", "gpt-6-sol"]
        self.prober._call = Mock(side_effect=[
            attempt("gpt-6-astra", ok=False), attempt("gpt-6-sol"),
        ])
        v = self.prober._reuse_shape(self.row, SECTION, self.shape)
        result = CandidateResult(self.row, {SECTION: v})
        plan = build_plan(self.row, result, {})
        self.assertEqual(plan.sections[SECTION].verified_models, ["gpt-6-sol"])


if __name__ == "__main__":
    unittest.main()
