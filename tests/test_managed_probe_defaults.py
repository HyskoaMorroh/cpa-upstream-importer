"""User-started probing works without a separately provisioned policy file."""
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import client, probe_policy
from test_probe_policy_transport import response


class ManagedProbeDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy_path = self.root / 'probe-policy.json'
        self.ledger = self.root / 'probe-policy.sqlite3'
        env = patch.dict(os.environ, {
            'IMPORTER_PROBE_POLICY_FILE': str(self.policy_path),
            'IMPORTER_PROBE_LEDGER': str(self.ledger),
            'IMPORTER_BACKUP_DIR': self.temp.name,
            'IMPORTER_PROBE_MODE': '',
        })
        env.start()
        self.addCleanup(env.stop)
        probe_policy.reset_policy_cache()
        self.addCleanup(probe_policy.reset_policy_cache)

    def test_missing_policy_does_not_disable_a_user_started_request(self):
        with patch.object(client, '_opener') as opener:
            opener.return_value.open.return_value = response()
            got = client.send('https://fixture.invalid/v1/models',
                              headers={'Authorization': 'Bearer fixture-key'},
                              body=b'', method='GET')
        self.assertEqual('200', got.status)
        opener.return_value.open.assert_called_once()
        self.assertTrue(self.ledger.is_file())
        self.assertFalse(self.policy_path.exists())

    def test_managed_mode_keeps_shared_limits_between_credentials(self):
        policy = probe_policy.get_policy()
        permit = policy.reserve('https://fixture.invalid/v1/models',
                                {'Authorization': 'Bearer first-key'})
        self.assertTrue(permit.allowed, permit.reason)
        denied = probe_policy.get_policy().reserve('https://fixture.invalid/v1/models',
                                                 {'Authorization': 'Bearer second-key'})
        self.assertFalse(denied.allowed)
        self.assertEqual('inflight', denied.code)
        self.assertTrue(policy.finish(permit, '200'))
        denied = policy.check('https://fixture.invalid/v1/models',
                              {'Authorization': 'Bearer second-key'})
        self.assertEqual('minimum_interval', denied.code)

    def test_missing_policy_summary_describes_managed_probing(self):
        summary = probe_policy.get_policy().summary()
        self.assertEqual('managed', summary['mode'])
        self.assertFalse(summary['default_passive'])
        self.assertFalse(summary.get('ledger_blocked'))

    def test_explicit_passive_mode_still_sends_nothing(self):
        with patch.dict(os.environ, {'IMPORTER_PROBE_MODE': 'passive'}):
            with patch.object(client, '_opener') as opener:
                got = client.send('https://fixture.invalid/v1/models',
                                  headers={}, body=b'', method='GET')
        self.assertEqual('passive', got.policy_code)
        opener.assert_not_called()
        self.assertFalse(self.ledger.exists())

    def test_invalid_policy_is_not_silently_replaced_by_managed_defaults(self):
        self.policy_path.write_text('{broken', encoding='utf-8')
        decision = probe_policy.get_policy().check('https://fixture.invalid', {})
        self.assertFalse(decision.allowed)
        self.assertEqual('invalid_policy', decision.code)

    def test_existing_explicit_allowlist_is_respected(self):
        self.policy_path.write_text(json.dumps({'version': 1, 'sites': {
            'configured.invalid': {'enabled': True, 'provider': 'configured',
                'max_requests': 5, 'max_requests_per_credential': 2,
                'window_seconds': 60, 'min_interval_seconds': 0}}}), encoding='utf-8')
        decision = probe_policy.get_policy().check('https://other.invalid', {})
        self.assertFalse(decision.allowed)
        self.assertEqual('unauthorized_site', decision.code)

    def test_unwritable_ledger_is_reported_and_never_bypassed(self):
        with patch.dict(os.environ, {'IMPORTER_PROBE_LEDGER': str(self.root / 'absent' / 'ledger')}):
            summary = probe_policy.get_policy().summary()
            decision = probe_policy.get_policy().check('https://fixture.invalid', {})
        self.assertTrue(summary.get('ledger_blocked'))
        self.assertEqual('ledger_unavailable', decision.code)

    def test_provider_restriction_survives_policy_reload(self):
        policy = probe_policy.get_policy()
        permit = policy.reserve('https://fixture.invalid', {'x-api-key': 'first-key'})
        self.assertTrue(permit.allowed, permit.reason)
        self.assertTrue(policy.finish(permit, '403', 'automated probing forbidden'))
        probe_policy.reset_policy_cache()
        decision = probe_policy.get_policy().check('https://fixture.invalid', {'x-api-key': 'second-key'})
        self.assertFalse(decision.allowed)
        self.assertEqual('provider_stopped', decision.code)


if __name__ == '__main__':
    unittest.main()
