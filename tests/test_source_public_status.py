"""Public source evidence remains explicit and does not disclose fetch errors."""
import json
import unittest
from unittest.mock import patch
from cpa_probe import cpa_source_probe as csp


class SourcePublicStatusTests(unittest.TestCase):
    def identity(self):
        return csp.CpaIdentity(
            errors=['https://private.invalid/?token=fixture-secret'],
            uncertainty=['fixture-secret'], coverage={'private.invalid': 'covered'},
            revisions={'private.invalid': 'fixture-secret', 'cpa': 'a' * 40},
            snapshot_id='fixture-secret', checked_at=123, expires_at=456)

    def test_public_status_filters_private_evidence(self):
        result = csp.source_status(self.identity())
        encoded = json.dumps(result)
        self.assertNotIn('private.invalid', encoded)
        self.assertNotIn('fixture-secret', encoded)
        self.assertEqual(result['revisions'], {'cpa': 'a' * 40})
        self.assertTrue(result['errors'])
        self.assertTrue(result['uncovered'])

    def test_body_partial_is_visible_when_all_capabilities_are_covered(self):
        ident = csp.CpaIdentity()
        ident.coverage = {key: 'covered' for key in csp.source_status(ident)['coverage']}
        ident.coverage['body:codex-api-key'] = 'partial'
        ident.coverage['file:private.invalid/token=fixture-secret'] = 'missing'
        result = csp.source_status(ident)
        self.assertTrue(result['partial'])
        self.assertEqual(result['body_coverage']['codex-api-key'], 'partial')
        self.assertIn('body:codex-api-key', result['uncovered'])
        self.assertTrue(result['errors'])
        self.assertNotIn('private.invalid', json.dumps(result))
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_config_fallback_retains_failed_snapshot_metadata(self):
        with patch.object(csp, 'extract_remote', return_value=self.identity()):
            result = csp.check(cfg={'claude-header-defaults': {}}, allow_remote=True)
        self.assertTrue(result['checked'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['checked_at'], 123)
        self.assertEqual(result['expires_at'], 456)
        self.assertTrue(result['remote_failed'])
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_failed_check_retains_diagnostics(self):
        with patch.object(csp, 'extract_remote', return_value=self.identity()):
            result = csp.check(allow_remote=True)
        self.assertFalse(result['checked'])
        self.assertEqual(result['checked_at'], 123)
        self.assertTrue(result['errors'])
        self.assertNotIn('private.invalid', json.dumps(result))

    def test_config_fallback_preserves_all_uncovered_source_capabilities(self):
        identity = self.identity()
        expected = csp.source_status(identity)['uncovered']
        with patch.object(csp, 'extract_remote', return_value=identity):
            result = csp.check(cfg={'claude-header-defaults': {}}, allow_remote=True)
        self.assertEqual(result['uncovered'], expected)
        self.assertGreater(len(result['uncovered']), 1)
        self.assertIn('仅核对 claude-header-defaults', result['source'])


if __name__ == '__main__':
    unittest.main()
