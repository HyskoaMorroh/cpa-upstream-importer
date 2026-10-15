import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cpa_probe import runtime_health as rh


class RuntimeHealthShapeTests(unittest.TestCase):
    def fetch(self, payload):
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(rh.urllib.request, 'build_opener', return_value=opener):
            return rh.fetch_cpa_runtime_health('http://localhost:8317')

    def test_malformed_statistics_fail_closed_without_crashing(self):
        for payload in ({'provider': []}, {'provider': {'key': None}},
                        {'provider': {'key': {'success': True}}},
                        {'provider': {'key': {'failed': -1}}},
                        {'provider': {'key': {'recent_requests': [None]}}},
                        {'provider': {'key': {'recent_requests': [{'success': '1'}]}}}):
            with self.subTest(payload=payload):
                self.assertIsNone(self.fetch(payload))

    def test_valid_statistics_preserve_legacy_score(self):
        result = self.fetch({'provider': {'key': {'success': 3, 'failed': 1,
                                                'recent_requests': [{'success': 1}, {}]}}})
        self.assertAlmostEqual(result['provider']['key'].health_score, .65)

    def test_compat_provider_uses_actual_registered_name(self):
        plan = SimpleNamespace(base_url='https://example.test/', api_key='test-key',
                               provider_name='Custom-Compat')
        health = rh.AuthHealth(3, 1, [])
        data = {'custom-compat': {'https://example.test|test-key': health}}
        self.assertIs(rh.match_auth_to_plan(plan, data, 'openai-compatibility'), health)
        plan.provider_name = 'different-provider'
        self.assertIsNone(rh.match_auth_to_plan(plan, data, 'openai-compatibility'))


if __name__ == '__main__':
    unittest.main()
