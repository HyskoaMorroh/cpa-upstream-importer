"""Offline restriction scope checks; only synthetic URLs and temporary ledgers."""
import sqlite3
import unittest
from contextlib import closing

from cpa_probe.classify import classify
import test_probe_policy as fixtures
from test_probe_policy import URL, HEADERS, configuration


class CatalogRestrictionScopeTests(unittest.TestCase):
    setUp = fixtures.ProbePolicyTests.setUp
    engine = fixtures.ProbePolicyTests.engine

    def settle(self, status, body):
        token = self.module._REQUEST_KIND.set('model_catalog')
        self.addCleanup(self.module._REQUEST_KIND.reset, token)
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed)
        self.assertTrue(engine.finish(permit, status, body))
        return engine

    def evidence_count(self):
        with closing(sqlite3.connect(self.ledger)) as db:
            return db.execute('SELECT COUNT(*) FROM restriction_evidence').fetchone()[0]

    def test_catalog_401_credential_limit_does_not_ban_other_credentials(self):
        engine = self.settle('401', '{"error":"当前密钥访问受限"}')
        self.assertEqual(classify('401', '当前密钥访问受限')[0], '鉴权')
        self.assertEqual(self.evidence_count(), 0)
        other = {'Authorization': 'Bearer synthetic-other-key'}
        self.assertTrue(engine.check(URL.replace('/models', '/responses'), other).allowed)

    def test_catalog_permission_does_not_prove_inference_failure(self):
        engine = self.settle('401', '{"error":"模型目录访问受限"}')
        self.assertEqual(classify('401', '模型目录访问受限')[0], '门禁')
        self.assertEqual(self.evidence_count(), 0)
        self.assertTrue(engine.check(URL.replace('/models', '/responses'), HEADERS).allowed)

    def test_catalog_403_keeps_job_stop_without_permanent_provider_evidence(self):
        engine = self.settle('403', '{"error":"模型目录访问受限"}')
        self.assertEqual(self.evidence_count(), 0)
        self.assertEqual(engine.check(URL, HEADERS).code, 'provider_job_stopped')

    def test_explicit_refusal_wins_over_credential_scope(self):
        # Each response uses a separate temporary ledger, with all network blocked.
        for suffix in ('访问已被拦截', '人机验证', 'cf-mitigated: challenge',
                       'unauthorized client detected', '反测活',
                       'automated probing forbidden', 'account suspended',
                       '全站访问受限', '访问受限'):
            with self.subTest(suffix=suffix):
                old = self.ledger
                self.ledger = str(self.root / ('case-' + str(len(list(self.root.glob('case-*')))) + '.sqlite3'))
                engine = self.settle('401', '当前密钥访问受限; ' + suffix)
                self.assertEqual(self.evidence_count(), 1)
                self.assertEqual(engine.check(URL, HEADERS).code, 'provider_stopped')
                self.ledger = old

    def test_negated_credential_clause_does_not_hide_explicit_refusal(self):
        engine = self.settle('401', '不是密钥访问受限; only official clients are allowed')
        self.assertEqual(self.evidence_count(), 1)
        self.assertEqual(engine.check(URL, HEADERS).code, 'provider_stopped')

    def test_plain_auth_and_unscoped_restriction_stay_distinct(self):
        self.assertEqual(classify('401', 'invalid API key')[0], '鉴权')
        self.assertEqual(classify('401', '访问受限')[0], 'WAF')
        self.assertEqual(classify('401', 'credential access restricted')[0], '鉴权')
        self.assertEqual(classify('403', 'models endpoint permission denied')[0], '门禁')
        self.assertEqual(classify('401', '当前密钥访问受限; 模型目录访问受限')[0], '鉴权')
        self.assertEqual(classify('401', '模型目录访问受限; 全站访问受限')[0], 'WAF')

    def test_new_classifier_does_not_erase_existing_evidence(self):
        engine = self.settle('401', 'unauthorized client detected')
        self.assertEqual(self.evidence_count(), 1)
        self.clock.return_value = 10000
        self.assertEqual(self.engine().check(URL, HEADERS).code, 'provider_stopped')
        self.assertEqual(self.evidence_count(), 1)


if __name__ == '__main__':
    unittest.main()
