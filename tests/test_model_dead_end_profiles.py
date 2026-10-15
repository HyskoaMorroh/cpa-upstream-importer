"""Model-specific channel failures must advance candidates, not repeat identities."""
import unittest
from unittest.mock import Mock, PropertyMock, patch

from cpa_probe.classify import classify
from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import Attempt, Prober, ProbePolicyStop


SECTION = "claude-api-key"
NO_CHANNEL = "No available channel for model model-a under group default"


def attempt(model, status="503", body=NO_CHANNEL):
    category, action = classify(status, body)
    return Attempt(section=SECTION, model=model, combo="baseline", status=status,
                   category=category, action=action, elapsed_ms=1, excerpt=body,
                   response_valid=status == "200")


class ModelDeadEndProfileTests(unittest.TestCase):
    def setUp(self):
        self.prober = object.__new__(Prober)
        proxy_patch = patch.object(Prober, "live_proxy", new_callable=PropertyMock, return_value=None)
        proxy_patch.start()
        self.addCleanup(proxy_patch.stop)
        self.prober._TRANSIENT_RETRIES = 0
        self.prober.on_event = Mock()
        self.prober._stage0_catalog = Mock(return_value=[])
        self.prober._probe_order = Mock(return_value=["model-a", "model-b", "model-c"])
        self.prober._try_profiles = Mock(return_value=False)
        self.prober._accept = Mock(side_effect=lambda v, model, att: [model])
        self.row = ParsedRow(1, "", "https://upstream.example", "fixture-key")
        self.catalog_patch = patch("cpa_probe.pipeline.model_catalog.note_catalog")
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)

    def test_model_503_skips_profiles_and_reaches_spare_success(self):
        self.prober._call = Mock(side_effect=[attempt("model-a"), attempt("model-b"),
                                               attempt("model-c", "200", "ok")])
        result = self.prober._stage1(self.row, SECTION)
        self.prober._try_profiles.assert_not_called()
        self.assertEqual([c.args[3] for c in self.prober._call.call_args_list],
                         ["model-a", "model-b", "model-c"])
        self.assertTrue(result.usable)
        self.assertEqual(result.models, ["model-c"])

    def test_all_model_failures_keep_diagnosis_without_becoming_usable(self):
        self.prober._call = Mock(side_effect=lambda *a, **kw: attempt(a[3]))
        result = self.prober._stage1(self.row, SECTION)
        self.prober._try_profiles.assert_not_called()
        self.assertFalse(result.usable)
        self.assertEqual(result.category, "死路")
        self.assertEqual(result.models, [])

    def test_generic_503_still_reaches_profiles(self):
        self.prober._call = Mock(side_effect=lambda *a, **kw: attempt(a[3], body="Service unavailable"))
        self.prober._stage1(self.row, SECTION)
        self.assertEqual(self.prober._try_profiles.call_count, 2)

    def test_explicit_beta_retains_profile_recovery(self):
        for status in ("400", "403", "503"):
            with self.subTest(status=status):
                self.prober._try_profiles.reset_mock()
                body = NO_CHANNEL + "; missing required header: anthropic-beta output-128k-2025-02-19"
                self.prober._call = Mock(side_effect=lambda *a, **kw: attempt(a[3], status, body))
                self.prober._stage1(self.row, SECTION)
                self.assertGreater(self.prober._try_profiles.call_count, 0)

    def test_policy_stop_is_not_swallowed_or_retried(self):
        self.prober._call = Mock(side_effect=ProbePolicyStop("provider_restriction", "fixture stop"))
        with self.assertRaises(ProbePolicyStop):
            self.prober._stage1(self.row, SECTION)
        self.prober._call.assert_called_once()
        self.prober._try_profiles.assert_not_called()


if __name__ == "__main__":
    unittest.main()
