"""Retry advice must not turn unavailable capacity into writable CPA limits."""
from types import SimpleNamespace
import unittest

from cpa_probe import tuning


def config(budget=11, count=4):
    return {
        "request-retry": 1, "max-retry-credentials": budget,
        "max-retry-interval": 8, "debug": True,
        "claude-api-key": [
            {"api-key": f"test-{i}", "base-url": f"https://s{i}.example",
             "priority": 900} for i in range(count)],
    }


def results(success=False):
    attempts = [SimpleNamespace(ok=False, elapsed_ms=80060) for _ in range(104)]
    if success:
        attempts.append(SimpleNamespace(ok=True, elapsed_ms=1500))
    return {"test": SimpleNamespace(sections={
        "claude": SimpleNamespace(attempts=attempts)})}


class RetryAdviceSafetyTests(unittest.TestCase):
    def assert_no_retry_write(self, advices):
        self.assertFalse([a for a in advices if a.changed and a.label in
                          ("max-retry-credentials", "max-retry-interval")])

    def test_all_failure_p90_is_diagnostic_only(self):
        sample = results()
        sec, why = tuning.attempt_seconds_from_results(sample)
        self.assertEqual(sec, 80.06)
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample))
        advices, notes, _ = tuning.advise(
            config(), attempt_sec=sec, attempt_why=why,
            timing_evidence_sufficient=tuning.retry_timing_evidence_sufficient(sample))
        self.assert_no_retry_write(advices)
        self.assertTrue(any("证据不足" in note for note in notes))
        self.assertTrue(any(a.label == "debug" and a.want is False for a in advices))

    def test_zero_capacity_is_not_unlimited_budget_even_with_mixed_samples(self):
        sample = results(success=True)
        self.assertTrue(tuning.retry_timing_evidence_sufficient(sample))
        sec, why = tuning.attempt_seconds_from_results(sample)
        advices, notes, _ = tuning.advise(
            config(), attempt_sec=sec, attempt_why=why, timing_evidence_sufficient=True)
        self.assert_no_retry_write(advices)
        self.assertTrue(any("0 在 CPA" in note for note in notes))

    def test_positive_insufficient_capacity_does_not_truncate_pool(self):
        advices, notes, _ = tuning.advise(
            config(budget=40, count=40), attempt_sec=2.9,
            timing_evidence_sufficient=True)
        self.assert_no_retry_write(advices)
        self.assertTrue(any("冲突" in note for note in notes))

    def test_existing_zero_means_unlimited_and_is_preserved(self):
        advices, _, _ = tuning.advise(
            config(budget=0), attempt_sec=2, timing_evidence_sufficient=True)
        self.assert_no_retry_write(advices)

    def test_unvalidated_and_fallback_latency_never_write_retry_limits(self):
        for kwargs in ({}, {"attempt_sec": 2, "attempt_why": "4 次失败尝试的 90 分位"}):
            advices, _, _ = tuning.advise(config(budget=3), **kwargs)
            self.assert_no_retry_write(advices)

    def test_validated_feasible_positive_budget_can_cover_pool(self):
        advices, _, _ = tuning.advise(
            config(budget=3), attempt_sec=2, timing_evidence_sufficient=True)
        budget = next(a for a in advices if a.label == "max-retry-credentials")
        self.assertEqual(budget.want, 4)
        self.assertEqual(budget.severity, "blocker")

    def test_success_only_has_no_failure_latency_evidence(self):
        sample = {"test": SimpleNamespace(sections={"claude": SimpleNamespace(
            attempts=[SimpleNamespace(ok=True, elapsed_ms=1000)])})}
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample))

    def test_success_from_other_provider_cannot_validate_failed_provider(self):
        sample = results()
        sample["other"] = SimpleNamespace(sections={"claude": SimpleNamespace(
            attempts=[SimpleNamespace(ok=True, elapsed_ms=1000)])})
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample))

    def test_production_evidence_requires_all_top_credentials(self):
        cfg = config(count=2)
        sample = {}
        for i in range(2):
            sample[str(i)] = SimpleNamespace(
                row=SimpleNamespace(bare=f"https://s{i}.example", api_key=f"test-{i}"),
                sections={"claude-api-key": SimpleNamespace(attempts=[
                    SimpleNamespace(ok=True, elapsed_ms=1000),
                    SimpleNamespace(ok=False, elapsed_ms=2000)])})
        self.assertTrue(tuning.retry_timing_evidence_sufficient(sample, cfg))
        sample.pop("1")
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample, cfg))

    def test_interval_can_be_reduced_when_attempts_fit_without_wait(self):
        cfg = config(budget=4)
        cfg["max-retry-interval"] = 100
        advices, _, _ = tuning.advise(cfg, attempt_sec=2, timing_evidence_sufficient=True)
        interval = next(a for a in advices if a.label == "max-retry-interval")
        self.assertEqual(interval.want, 74)
        self.assertIn("P90 不是严格上界", interval.why)
        self.assertFalse(any(a.label == "max-retry-credentials" for a in advices))

    def test_same_host_key_different_base_paths_need_separate_evidence(self):
        cfg = config(count=1)
        entry = cfg["claude-api-key"][0]
        entry["base-url"] = "https://s0.example/team-a"
        cfg["claude-api-key"].append(dict(entry, **{"base-url": "https://s0.example/team-b"}))
        verdict = SimpleNamespace(attempts=[SimpleNamespace(ok=True, elapsed_ms=1000),
                                           SimpleNamespace(ok=False, elapsed_ms=2000)])
        sample = {"a": SimpleNamespace(row=SimpleNamespace(
            bare="https://S0.EXAMPLE/team-a/", api_key="test-0"),
            sections={"claude-api-key": verdict})}
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample, cfg))
        sample["b"] = SimpleNamespace(row=SimpleNamespace(
            bare="https://s0.example/team-b", api_key="test-0"),
            sections={"claude-api-key": verdict})
        self.assertTrue(tuning.retry_timing_evidence_sufficient(sample, cfg))
        sample["b"].row.bare = "http://s0.example/team-b"
        self.assertFalse(tuning.retry_timing_evidence_sufficient(sample, cfg))

    def test_no_zero_to_zero_interval_advice(self):
        cfg = config(budget=4)
        cfg["max-retry-interval"] = 0
        advices, _, _ = tuning.advise(cfg, attempt_sec=11.25, timing_evidence_sufficient=True)
        self.assertFalse(any(a.label == "max-retry-interval" for a in advices))

    def test_unlimited_budget_cannot_be_calculated_as_zero_attempts(self):
        with self.assertRaises(ValueError):
            tuning.worst_case_seconds(budget=0, rounds=2, attempt_sec=2, interval_sec=8)


if __name__ == "__main__":
    unittest.main()
