"""Explicit credential errors and failed profiles must not become writeback evidence."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import parse_lines
from cpa_probe.classify import classify
from cpa_probe.pipeline import Attempt, Prober, SectionVerdict


class CredentialClassificationTests(unittest.TestCase):
    def test_explicit_invalid_key_400(self):
        for body in ('API key not valid. Please pass a valid API key.',
                     'Invalid API key', 'api_key_invalid'):
            with self.subTest(body=body):
                self.assertEqual(('鉴权', 'API key 无效'), classify('400', body))

    def test_unknown_400_remains_unknown(self):
        self.assertEqual('未知', classify('400', 'Unspecified bad input')[0])

    def test_successful_content_is_not_a_credential_error(self):
        self.assertEqual('可用', classify('200', 'API key not valid')[0])

    def test_specific_restrictions_keep_precedence(self):
        self.assertEqual('WAF', classify('403', '访问受限; API key not valid')[0])
        self.assertEqual(('死路', 'Key 分组不匹配'),
                         classify('400', 'api key group mismatch; invalid API key'))


class ProfileEvidenceTests(unittest.TestCase):
    def run_profile(self, baseline_category, status, category, accepted=False):
        p = object.__new__(Prober)
        p.reuse_profile_verdict = False
        p.cfg_snapshot = {}
        p._evidence_key = Mock(return_value=('fixture',))
        p._lock = threading.Lock()
        p._profiles_failed = set()
        p.on_event = Mock()
        p._retry_with_betas = Mock(return_value=False)
        p._accept = Mock(return_value=['fixture-model'] if accepted else [])
        p._call = Mock(return_value=Attempt(
            section='claude-api-key', model='fixture-model', combo='id:fixture',
            status=status, category=category, action='fixture error', elapsed_ms=1,
            response_valid=status == '200'))
        prof = SimpleNamespace(name='fixture-profile', is_baseline=False,
                               tier=1, family='fixture',
                               body_patch={'metadata': {'user_id': 'user_fixture'}})
        v = SectionVerdict('claude-api-key', attempts=[Attempt(
            section='claude-api-key', model='fixture-model', combo='base',
            status='401', category=baseline_category, action='baseline', elapsed_ms=1)])
        row = parse_lines('https://fixture.invalid,fixture-key', allow_private=True).valid[0]
        with patch('cpa_probe.pipeline.profiles.ladder', return_value=[prof]), \
             patch('cpa_probe.pipeline.profiles.materialize',
                   return_value=({'X-Fixture': 'unproven'}, prof.body_patch)):
            result = p._try_profiles(row, v.section, row.bare, 'fixture-model', v)
        return result, v, p

    def test_failed_profile_never_proves_identity_or_supplies_writeback_fields(self):
        for baseline, status, category in [('鉴权', '401', '鉴权'),
                                            ('客户端', '401', '鉴权'),
                                            ('客户端', '503', '临时'),
                                            ('门禁', '400', '鉴权')]:
            with self.subTest(baseline=baseline, status=status):
                result, v, p = self.run_profile(baseline, status, category)
                self.assertFalse(result)
                self.assertFalse(v.usable)
                self.assertFalse(v.identity_proven)
                self.assertEqual({}, v.min_headers)
                self.assertEqual('', v.profile_name)
                self.assertEqual('', v.min_body_kind)
                self.assertEqual(category, v.category)
                self.assertIn('身份尚未验证', v.action)
                self.assertNotIn('已被站方接受', v.action)
                self.assertNotIn('profile-gate-passed', [c.args[0] for c in p.on_event.call_args_list])

    def test_valid_but_unaccepted_response_does_not_prove_profile(self):
        result, v, _ = self.run_profile('客户端', '200', '可用')
        self.assertFalse(result)
        self.assertFalse(v.identity_proven)
        self.assertEqual({}, v.min_headers)

    def test_accepted_response_retains_successful_profile(self):
        result, v, _ = self.run_profile('客户端', '200', '可用', accepted=True)
        self.assertTrue(result)
        self.assertTrue(v.usable)
        self.assertEqual('fixture-profile', v.profile_name)
        self.assertEqual({'X-Fixture': 'unproven'}, v.min_headers)
        self.assertEqual('metadata.user_id(plain)', v.min_body_kind)
        self.assertEqual(['fixture-model'], v.models)


if __name__ == '__main__':
    unittest.main()
