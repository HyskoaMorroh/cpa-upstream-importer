"""Offline last-good persistence, privacy and refresh contracts."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cpa_probe import cpa_source_probe as csp


class LastGoodSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'CPA_SOURCE_CACHE_DIR': self.directory.name})
        self.env.start()
        csp._remote_cache.clear()
        csp._last_good_cache.clear()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.env.stop)
        self.addCleanup(csp._remote_cache.clear)
        self.addCleanup(csp._last_good_cache.clear)

    def good(self):
        return csp.CpaIdentity(
            claude_betas_unconditional=['public-beta'],
            codex_user_agent='public-client', codex_originator='public-origin',
            coverage={k: 'covered' for k in csp.source_status(csp.CpaIdentity())['coverage']},
            snapshot_id='b' * 64, checked_at=time.time(), expires_at=time.time()+3600,
            revisions={k: 'a' * 40 for k in csp.SOURCE_MANIFEST}, immutable=True,
            source_root='do-not-persist-private-path', uncertainty=['private-error'],
            body_shape={'codex-api-key': ({'stream': 'true'}, ['metadata'])})

    def fetch(self, requests, *args):
        return {name: ((200, json.dumps({'sha': 'a' * 40})) if isinstance(name, str)
                       else (200, 'public fixture')) for name, _ in requests}

    def prime(self):
        with patch.object(csp, '_fetch_manifest_batch', side_effect=self.fetch), \
             patch.object(csp, '_parse_sources', side_effect=lambda *a, **kw: self.good()):
            return csp.extract_remote(proxy='http://user:private-proxy@localhost:1')

    def fail(self, **kwargs):
        with patch.object(csp, '_fetch_manifest_batch', return_value={}):
            return csp.extract_remote(**kwargs)

    def test_restart_refresh_failure_retains_constants_and_stale_evidence(self):
        original = self.prime()
        csp._remote_cache.clear()
        csp._last_good_cache.clear()
        with patch.object(csp, '_fetch_manifest_batch', return_value={}) as fetch:
            stale = csp.extract_remote()
        self.assertTrue(fetch.called)  # restart always attempts a network refresh
        self.assertEqual(stale.claude_betas_unconditional, ['public-beta'])
        self.assertFalse(stale.ok)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.checked_at, original.checked_at)
        self.assertEqual(stale.expires_at, original.expires_at)
        status = csp.source_status(stale)
        self.assertTrue(status['partial'])
        self.assertTrue(status['errors'])
        self.assertTrue(status['stale'])
        self.assertGreater(status['refresh_failed_at'], 0)
        self.assertFalse(original.stale)
        self.assertTrue(original.ok)

    def test_real_parser_coverage_keys_allow_complete_snapshot_persistence(self):
        ident = self.good()
        ident.coverage.update({f'file:{repo}:{path}': 'covered'
                               for repo, paths in csp.SOURCE_MANIFEST.items() for path in paths})
        ident.coverage.update({'body:' + section: 'covered'
                               for section in csp._BODY_SHAPE_SOURCES})
        self.assertTrue(csp._last_good_valid(ident))
        csp._last_good_save('fixture', ident)
        self.assertTrue(list(Path(self.directory.name).glob('*.json')))
        ident.coverage['body:codex-api-key'] = 'partial'
        self.assertFalse(csp._last_good_valid(ident))

    def test_failure_never_overwrites_persisted_good_and_retry_recovers(self):
        original = self.prime()
        files = list(Path(self.directory.name).glob('*.json'))
        before = files[0].read_bytes()
        original.expires_at = 0
        stale = self.fail(proxy='http://user:private-proxy@localhost:1')
        self.assertTrue(stale.stale)
        self.assertEqual(files[0].read_bytes(), before)
        stale._retry_after = 0
        recovered = self.prime()
        self.assertTrue(recovered.ok)
        self.assertFalse(recovered.stale)

    def test_persisted_data_excludes_transport_and_raw_diagnostics(self):
        self.prime()
        text = next(Path(self.directory.name).glob('*.json')).read_text(encoding='utf-8')
        for private in ('private-proxy', 'do-not-persist', 'private-error', 'http://'):
            self.assertNotIn(private, text)
        self.assertNotIn('_remote_key', text)
        self.assertNotIn('main', text)

    def test_ref_manifest_extractor_and_repository_mismatch_do_not_reuse(self):
        self.prime()
        csp._remote_cache.clear()
        self.assertFalse(self.fail(ref='different').stale)
        self.assertFalse(self.fail(cpamp_ref='different').stale)
        self.assertFalse(self.fail(sub2api_ref='different').stale)
        with patch.object(csp, '_SOURCE_EXTRACTOR_VERSION', 999):
            self.assertFalse(self.fail().stale)
        with patch.dict(csp.SOURCE_MANIFEST, {'cpa': ('different.go',)}):
            self.assertFalse(self.fail().stale)
        with patch.object(csp, '_RAW_BASE', 'https://raw.githubusercontent.com/public/other'):
            self.assertFalse(self.fail().stale)

    def test_corrupt_wrong_schema_and_invalid_types_are_ignored(self):
        self.prime()
        path = next(Path(self.directory.name).glob('*.json'))
        valid = json.loads(path.read_text(encoding='utf-8'))
        invalid = [b'{broken', json.dumps({'schema': 999}).encode()]
        for field, value in [('revisions', []), ('coverage', 'covered'), ('immutable', 'true'),
                             ('checked_at', 'yesterday'), ('snapshot_id', 45),
                             ('coverage', {'codex-identity': 'covered'}),
                             ('claude_betas_unconditional', [12]),
                             ('body_shape', {'codex-api-key': [None, []]}),
                             ('body_rules', {'codex-api-key': [{}]}),
                             ('cpamp_disable_semantics', {'bad': float('nan')})]:
            payload = copy.deepcopy(valid)
            payload['identity'][field] = value
            invalid.append(json.dumps(payload).encode())
        for payload in invalid:
            with self.subTest(payload=payload[:50]):
                path.write_bytes(payload)
                csp._remote_cache.clear()
                csp._last_good_cache.clear()
                self.assertFalse(self.fail().stale)

    def test_exception_uses_last_good_without_exposing_exception(self):
        self.prime()
        csp._remote_cache.clear()
        with patch.object(csp, '_fetch_manifest_batch', side_effect=RuntimeError('secret URL')):
            result = csp.extract_remote()
        self.assertTrue(result.stale)
        self.assertNotIn('secret URL', json.dumps(csp.source_status(result)))

    def test_default_config_fallback_has_complete_public_contract(self):
        result = csp.check(source_root='', cfg={'claude-header-defaults': {}})
        self.assertTrue(result['checked'])
        self.assertTrue(result['partial'])
        self.assertEqual(result['uncovered'], list(result['coverage']))


if __name__ == '__main__':
    unittest.main()
