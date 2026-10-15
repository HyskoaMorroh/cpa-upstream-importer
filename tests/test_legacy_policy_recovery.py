"""Legacy stop recovery uses temporary ledgers and never sends network traffic."""
import sqlite3
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import test_probe_policy as fixtures
from test_probe_policy import URL, HEADERS, configuration


class LegacyRecoveryTests(unittest.TestCase):
    setUp = fixtures.ProbePolicyTests.setUp
    engine = fixtures.ProbePolicyTests.engine
    send = fixtures.ProbePolicyTests.send
    deny = fixtures.ProbePolicyTests.deny
    def legacy(self, reason="provider_restriction"):
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        permit = self.send(engine)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE providers SET stopped=?", (reason,))
            db.execute("UPDATE reservations SET outcome=? WHERE id=?", (reason, permit.reservation_id))
        self.clock.return_value = 2000
        return engine, permit.reservation_id

    def scope(self, name):
        token = self.module._JOB_SCOPE.set(name)
        self.addCleanup(self.module._JOB_SCOPE.reset, token)

    def test_legacy_requires_explicit_job_and_preserves_audit(self):
        engine, old = self.legacy()
        self.deny(engine.check(URL, HEADERS), "provider_stopped")
        self.scope("user-reprobe")
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed)
        self.assertTrue(engine.finish(permit, "200", '{"data":[]}'))
        self.assertTrue(engine.check(URL, HEADERS).allowed)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("SELECT stopped FROM providers").fetchone()[0], "provider_restriction")
            self.assertEqual(db.execute("SELECT outcome FROM reservations WHERE id=?", (old,)).fetchone()[0], "provider_restriction")
            self.assertEqual(db.execute("SELECT outcome FROM recovery_attempts").fetchone()[0], "recovered")

    def test_uncertain_failure_is_once_per_job_and_cools_new_job(self):
        engine, _ = self.legacy()
        self.scope("first")
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed)
        self.assertTrue(engine.finish(permit, "404", "not found"))
        self.deny(engine.check(URL, HEADERS), "provider_job_stopped")
        self.scope("second")
        self.deny(self.engine().check(URL, HEADERS), "provider_cooldown")
        self.clock.return_value = 3000
        self.assertTrue(engine.reserve(URL, HEADERS).allowed)

    def test_pending_recovery_survives_reload_and_prevents_parallel_attempt(self):
        engine, _ = self.legacy()
        self.scope("first")
        self.assertTrue(engine.reserve(URL, HEADERS).allowed)
        self.scope("second")
        self.clock.return_value = 4000
        self.deny(self.engine().reserve(URL, HEADERS), "inflight")

    def test_unconfirmed_reason_is_specific_safe_and_does_not_reopen_job(self):
        engine, _ = self.legacy()
        self.scope("first")
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(engine.finish(permit, "000", "", "private diagnostic"))
        checked = engine.check(URL, HEADERS)
        reserved = engine.reserve(URL, HEADERS)
        self.deny(checked, "provider_job_stopped")
        self.assertEqual(checked.reason, reserved.reason)
        self.assertIn("旧限制记录复核未确认成功", checked.reason)
        self.assertIn("900 秒", checked.reason)
        self.assertNotIn("private diagnostic", checked.reason)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT inflight FROM providers").fetchone()[0], "")
        self.clock.return_value = 3000
        expired = engine.check(URL, HEADERS)
        self.deny(expired, "provider_job_stopped")
        self.assertIn("原任务不会自动恢复", expired.reason)
        self.assertNotIn("900 秒", expired.reason)

    def test_new_job_cooldown_reports_remaining_time_without_refreshing_it(self):
        engine, _ = self.legacy()
        self.scope("first")
        self.send(engine, status="404", body="not found")
        self.scope("second")
        self.clock.return_value = 2000.25
        checked = engine.check(URL, HEADERS)
        reserved = engine.reserve(URL, HEADERS)
        self.deny(checked, "provider_cooldown")
        self.assertEqual(checked.reason, reserved.reason)
        self.assertIn("900 秒", checked.reason)
        self.clock.return_value = 2899.1
        self.assertIn("1 秒", engine.check(URL, HEADERS).reason)
        self.clock.return_value = 2900
        self.assertTrue(engine.check(URL, HEADERS).allowed)

    def test_unknown_job_stop_reason_is_never_echoed(self):
        engine, _ = self.legacy()
        self.scope("first")
        self.send(engine, status="404", body="not found")
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE job_stops SET reason=?", ("<private.example/key>",))
        decision = engine.check(URL, HEADERS)
        self.deny(decision, "provider_job_stopped")
        self.assertNotIn("private.example", decision.reason)
        self.assertNotIn("旧限制记录复核未确认成功", decision.reason)

    def test_unconfirmed_http_response_is_not_reported_as_no_response(self):
        engine, _ = self.legacy()
        self.scope("first")
        self.send(engine, status="429", body="rate limit")
        decision = engine.check(URL, HEADERS)
        self.assertIn("旧限制记录复核未确认成功", decision.reason)
        self.assertNotIn("未获得", decision.reason)
        self.assertNotIn("HTTP 000", decision.reason)

    def test_new_refusal_never_reopens_even_after_prior_recovery(self):
        engine, _ = self.legacy()
        self.scope("first")
        self.send(engine)
        self.send(engine, status="403", body="automated probing forbidden")
        self.scope("second")
        self.clock.return_value = 10000
        self.deny(self.engine().check(URL, HEADERS), "provider_stopped")

    def test_current_classifier_evidence_blocks_recovery(self):
        engine = self.engine()
        self.send(engine, status="403", body="cf-mitigated: challenge")
        self.scope("explicit-reprobe")
        self.clock.return_value = 10000
        self.deny(self.engine().check(URL, HEADERS), "provider_stopped")

    def test_specific_legacy_refusals_remain_stopped(self):
        engine, _ = self.legacy("restriction")
        self.scope("explicit-reprobe")
        self.deny(engine.check(URL, HEADERS), "provider_stopped")

    def test_concurrent_jobs_reserve_only_one_recovery(self):
        self.legacy()
        def reserve(index):
            token = self.module._JOB_SCOPE.set("job-" + str(index))
            try:
                return self.engine().reserve(URL, HEADERS)
            finally:
                self.module._JOB_SCOPE.reset(token)
        with ThreadPoolExecutor(max_workers=6) as pool:
            permits = list(pool.map(reserve, range(6)))
        self.assertEqual(sum(p.allowed for p in permits), 1)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recovery_attempts").fetchone()[0], 1)

    def test_success_envelope_error_does_not_recover(self):
        engine, _ = self.legacy()
        self.scope("explicit-reprobe")
        self.send(engine, status="200", body='{"success":false,"error":"unavailable"}')
        self.deny(engine.check(URL, HEADERS), "provider_job_stopped")

    def test_legacy_recovery_still_honors_budget(self):
        self.legacy()
        self.scope("explicit-reprobe")
        engine = self.engine(configuration(max_requests=1, window_seconds=10000))
        self.deny(engine.reserve(URL, HEADERS), "provider_budget")


    def catalog_missing(self):
        engine, _ = self.legacy()
        self.scope("catalog-job")
        with self.module.policy_request_kind("model_catalog"):
            self.send(engine, status="404", body="not found")
        return engine

    def test_catalog_404_allows_only_same_job_credential_inference(self):
        engine = self.catalog_missing()
        self.assertTrue(engine.check(URL, HEADERS).allowed)
        self.deny(engine.reserve(URL, HEADERS), "recovery_inference_required")
        with self.module.policy_request_kind("model_catalog"):
            for _ in range(3):
                self.deny(engine.reserve(URL, HEADERS), "recovery_catalog_skipped")
        with self.module.policy_request_kind("inference"):
            self.deny(engine.reserve(URL, {"Authorization": "Bearer other-synthetic"}), "recovery_inference_required")
            # Reopen ledger to prove the stage is persisted rather than in memory.
            restarted = self.engine()
            permit = restarted.reserve(URL, HEADERS)
            self.assertTrue(permit.allowed, permit.reason)
            self.deny(engine.reserve(URL, HEADERS), "inflight")
            self.assertTrue(restarted.finish(permit, "404", "model unavailable"))
            self.deny(engine.reserve(URL, HEADERS), "provider_job_stopped")
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(3, db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])
            self.assertEqual([("catalog_unavailable",), ("unconfirmed",)],
                             db.execute("SELECT outcome FROM recovery_attempts ORDER BY created_at, rowid").fetchall())
            self.assertEqual(2900, db.execute("SELECT until FROM cooldowns").fetchone()[0])

    def test_catalog_404_is_not_recovery_and_new_scope_cannot_borrow_stage(self):
        engine = self.catalog_missing()
        self.scope("different-job")
        with self.module.policy_request_kind("inference"):
            self.deny(engine.reserve(URL, HEADERS), "provider_cooldown")
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM recovery_attempts WHERE outcome='recovered'").fetchone()[0])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM job_stops").fetchone()[0])

    def test_catalog_404_refusal_stops_immediately(self):
        engine, _ = self.legacy()
        self.scope("catalog-job")
        with self.module.policy_request_kind("model_catalog"):
            self.send(engine, status="404", body="automated probing forbidden")
        with self.module.policy_request_kind("inference"):
            self.deny(engine.reserve(URL, HEADERS), "provider_stopped")

    def test_catalog_extension_keeps_budget(self):
        self.catalog_missing()
        engine = self.engine(configuration(max_requests=1, max_requests_per_credential=1))
        with self.module.policy_request_kind("inference"):
            self.deny(engine.reserve(URL, HEADERS), "provider_budget")

    def test_catalog_extension_success_settles_and_releases_admission(self):
        engine = self.catalog_missing()
        with self.module.policy_request_kind("inference"):
            self.send(engine, status="200", body='{"choices":[{"message":{"content":"synthetic"}}]}')
        self.assertTrue(engine.check(URL, HEADERS).allowed)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM recovery_attempts WHERE outcome='recovered'").fetchone()[0])
            self.assertEqual("", db.execute("SELECT inflight FROM providers").fetchone()[0])

    def test_request_scope_consumes_semantic_argument_and_resets_context(self):
        observed = []
        class Sender:
            @self.module.policy_request_scope
            def send(self, **kwargs):
                observed.append((fixtures.importlib.import_module("cpa_probe.probe_policy")._REQUEST_KIND.get(), kwargs))
        Sender().send(request_kind="inference", body="synthetic")
        self.assertEqual([("inference", {"body": "synthetic"})], observed)
        self.assertEqual("unknown", self.module._REQUEST_KIND.get())


    def pipeline_recovery(self, body, status="200"):
        from unittest.mock import patch
        from cpa_probe.pipeline import Prober, SectionVerdict, CandidateResult
        from cpa_probe.cpa_source_probe import CpaIdentity
        from cpa_probe.client import Response
        from cpa_probe.parse import ParsedRow
        from cpa_probe.plan import build_plan
        engine, _ = self.legacy()
        row = ParsedRow(1, "synthetic", "https://api.fixture.invalid", "synthetic-credential-one")
        observed = []
        events = []
        def transport(url, **kwargs):
            kind = self.module._REQUEST_KIND.get()
            observed.append((kind, kwargs.get("method", "POST")))
            permit = engine.reserve(url, kwargs["headers"])
            self.assertTrue(permit.allowed, permit.reason)
            response_status, response_body = ("404", "not found") if kind == "model_catalog" else (status, body)
            self.assertTrue(engine.finish(permit, response_status, response_body))
            return Response(response_status, response_body, 1)
        with patch("cpa_probe.cpa_source_probe.cached_identity", return_value=CpaIdentity()), \
                patch("cpa_probe.probe_policy.get_policy", return_value=engine), \
                patch("cpa_probe.pipeline.client.send", side_effect=transport):
            prober = Prober(gap=0, timeout=1, on_event=lambda k, d: events.append((k, d)))
            self.assertEqual([], prober._stage0_catalog(row, "openai-compatibility", row.bare))
            self.assertEqual([], prober._stage0_catalog(row, "openai-compatibility", row.bare))
            attempt = prober._call("openai-compatibility", row.bare, row.api_key, "synthetic-model", combo="baseline")
        self.assertEqual([("model_catalog", "GET"), ("inference", "POST")], observed)
        self.assertTrue(any(k == "catalog-miss" and d.get("status") == "unverified" for k, d in events))
        return prober, row, attempt, SectionVerdict, CandidateResult, build_plan

    def test_pipeline_catalog_404_continues_to_inference(self):
        _, _, attempt, _, _, _ = self.pipeline_recovery("upstream unavailable", "503")
        self.assertFalse(attempt.ok)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual([("catalog_unavailable",), ("unconfirmed",)], db.execute("SELECT outcome FROM recovery_attempts ORDER BY rowid").fetchall())
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM job_stops").fetchone()[0])

    def assert_invalid_pipeline_body(self, body):
        prober, row, attempt, Verdict, Result, build_plan = self.pipeline_recovery(body)
        self.assertFalse(attempt.ok)
        self.assertFalse(attempt.response_valid)
        verdict = Verdict("openai-compatibility", base_url=row.bare, attempts=[attempt])
        verdict.models = prober._accept(verdict, "synthetic-model", attempt)
        self.assertEqual([], verdict.models)
        result = Result(row, {verdict.section: verdict})
        self.assertFalse(build_plan(row, result, {}).any_writable)

    def test_pipeline_recovery_html200_cannot_be_written(self):
        self.assert_invalid_pipeline_body("<html><body>Service maintenance</body></html>")

    def test_pipeline_recovery_empty200_cannot_be_written(self):
        self.assert_invalid_pipeline_body("")

    def test_pipeline_recovery_invalid_json200_cannot_be_written(self):
        self.assert_invalid_pipeline_body("{broken-json")


if __name__ == "__main__":
    unittest.main()
