"""Partial snapshot persistence preserves evidence and request semantics."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from cpa_probe import cpa_source_probe as csp
from cpa_probe.request import build_request


class PartialSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'CPA_SOURCE_CACHE_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        for cache in (csp._remote_cache, csp._last_good_cache, csp._partial_snapshot_cache):
            cache.clear()
            self.addCleanup(cache.clear)

    def identity(self):
        now = time.time()
        coverage = {k: 'covered' for k in csp.source_status(csp.CpaIdentity())['coverage']}
        coverage.update({f'file:{repo}:{path}': 'covered'
                         for repo, paths in csp.SOURCE_MANIFEST.items() for path in paths})
        coverage.update({'body:' + section: 'partial' for section in csp._BODY_SHAPE_SOURCES})
        return csp.CpaIdentity(
            codex_user_agent='public-client', codex_originator='public-origin',
            claude_betas_unconditional=['public-beta'],
            claude_header_defaults={'User-Agent': 'public-claude'},
            coverage=coverage, revisions={k: 'a'*40 for k in csp.SOURCE_MANIFEST},
            snapshot_id='b'*64, checked_at=now, expires_at=now+300,
            immutable=True, source_root='private-path-marker', uncertainty=['private-error-marker'],
            body_shape={section: ({'temperature': '0.5'}, ['metadata'])
                        for section in csp._BODY_SHAPE_SOURCES},
            body_rules={section: [
                {'path': 'stream', 'op': 'set', 'value': 'true', 'conditions': {'stream': True}, 'unknown': []},
                {'path': 'temperature', 'op': 'set', 'value': '0.7',
                 'conditions': {'support-prompt-cache-key': True}, 'unknown': []},
                {'path': 'metadata', 'op': 'drop', 'value': '', 'conditions': {}, 'unknown': []}]
                        for section in csp._BODY_SHAPE_SOURCES})

    def save_restore(self):
        original = self.identity()
        csp._partial_snapshot_save('fixture', original)
        csp._partial_snapshot_cache.clear()
        restored = csp._last_good_fallback('fixture', csp.CpaIdentity())
        self.assertTrue(restored.stale)
        return original, restored

    def test_restart_preserves_request_outputs_all_sections_and_branches(self):
        original, restored = self.save_restore()
        for section in csp._BODY_SHAPE_SOURCES:
            for stream in (False, True):
                for cfg in ({}, {'support-prompt-cache-key': True}):
                    with self.subTest(section=section, stream=stream, cfg=cfg):
                        args = (section, 'https://example.invalid', 'fixture-model', 'fixture-key')
                        kwargs = dict(text='Return the result of 2+2.', stream=stream,
                                      cfg=cfg, entry_config={'prefix': 'fixture', **cfg}, session_id='fixed-session')
                        online = build_request(*args, source_identity=original, **kwargs)
                        offline = build_request(*args, source_identity=restored, **kwargs)
                        self.assertEqual(online, offline)
        self.assertEqual(restored.coverage, original.coverage)
        self.assertEqual(restored.snapshot_id, original.snapshot_id)
        self.assertEqual(restored.checked_at, original.checked_at)
        self.assertEqual(restored.expires_at, original.expires_at)
        self.assertFalse(restored.ok)
        self.assertLessEqual(restored._retry_after, original.expires_at)
        self.assertTrue(csp.source_status(restored)['partial'])

    def test_privacy_and_independent_tier(self):
        original, _ = self.save_restore()
        files = list(Path(self.temp.name).glob('*.json'))
        self.assertEqual([f.name for f in files], ['fixture.partial.json'])
        raw = files[0].read_text(encoding='utf-8')
        self.assertNotIn('private-', raw)
        self.assertIsNone(csp._last_good_load('fixture'))
        self.assertFalse(csp._last_good_valid(original))
        self.assertIsNone(csp._partial_snapshot_load('different-key'))

    def test_real_parser_rules_survive_disk_restart(self):
        original = self.identity()
        rules = csp.parse_body_rules('''func Execute() {
            body, _ = sjson.SetBytes(body, "temperature", 0.5)
            body = rewriteBody(body)
        }''')
        self.assertTrue(any(rule['op'] == 'opaque' for rule in rules))
        self.assertTrue(all('function' in rule and 'line' in rule for rule in rules))
        original.body_rules = {section: copy.deepcopy(rules) for section in csp._BODY_SHAPE_SOURCES}
        csp._partial_snapshot_save('parser', original)
        csp._partial_snapshot_cache.clear()
        restored = csp._last_good_fallback('parser', csp.CpaIdentity())
        self.assertTrue(restored.stale)
        self.assertFalse(restored.ok)
        self.assertEqual(restored.body_rules, original.body_rules)
        self.assertEqual(restored.snapshot_id, original.snapshot_id)
        self.assertEqual(restored.checked_at, original.checked_at)
        self.assertEqual(restored.expires_at, original.expires_at)
        for stream in (False, True):
            self.assertEqual(csp.resolve_body_rules(restored.body_rules['codex-api-key'], stream=stream),
                             csp.resolve_body_rules(rules, stream=stream))

    def test_malformed_parser_rule_metadata_rejected(self):
        self.save_restore()
        path = Path(self.temp.name)/'fixture.partial.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        rule = {'path': None, 'op': 'opaque', 'value': 'rewriteBody(body)',
                'conditions': {}, 'unknown': ['unresolved rewrite'],
                'function': 'Execute', 'line': 2}
        mutations = [{'line': True}, {'line': 0}, {'line': -1}, {'line': '2'},
                     {'function': []}, {'unknown': []}, {'path': 'body'}, {'op': 'unknown'}]
        for mutation in mutations + ['missing-line', 'missing-function']:
            bad = copy.deepcopy(payload)
            malformed = copy.deepcopy(rule)
            if isinstance(mutation, dict):
                malformed.update(mutation)
            else:
                del malformed[mutation.removeprefix('missing-')]
            bad['identity']['body_rules']['codex-api-key'] = [malformed]
            path.write_text(json.dumps(bad), encoding='utf-8')
            with self.subTest(mutation=mutation):
                self.assertIsNone(csp._partial_snapshot_load('fixture'))

    def test_expiry_never_renews(self):
        original, restored = self.save_restore()
        with patch.object(csp.time, 'time', return_value=original.expires_at):
            self.assertIsNone(csp._partial_snapshot_load('fixture'))
            failed = csp.CpaIdentity()
            self.assertIs(csp._last_good_fallback('fixture', failed), failed)
        self.assertEqual(restored.expires_at, original.expires_at)

    def test_missing_transport_or_capability_never_persisted(self):
        for change in ('errors', 'immutable', 'file', 'capability', 'future', 'longttl'):
            value = self.identity()
            if change == 'errors': value.errors = ['transport failure']
            elif change == 'immutable': value.immutable = False
            elif change == 'file': value.coverage[next(k for k in value.coverage if k.startswith('file:'))] = 'missing'
            elif change == 'capability': value.coverage['codex-identity'] = 'partial'
            elif change == 'future': value.checked_at = time.time()+1000
            else: value.expires_at = value.checked_at+601
            with self.subTest(change=change):
                csp._partial_snapshot_save(change, value)
                self.assertIsNone(csp._partial_snapshot_load(change))

    def test_malformed_disk_values_rejected(self):
        self.save_restore()
        path = Path(self.temp.name)/'fixture.partial.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        mutations = [('checked_at', True), ('expires_at', float('inf')),
                     ('immutable', 1), ('stale', 0), ('codex_user_agent', []),
                     ('cpamp_disable_semantics', {'compat_disable_value': 1}),
                     ('body_rules', {'codex-api-key': [{'path': 'stream', 'op': 'set', 'value': 'true',
                         'conditions': {'stream': 1}, 'unknown': []}]}),
                     ('body_shape', {'codex-api-key': ['bad', []]}),
                     ('revisions', {k: 'not-a-sha' for k in csp.SOURCE_MANIFEST})]
        for key, value in mutations:
            bad = copy.deepcopy(payload)
            bad['identity'][key] = value
            path.write_text(json.dumps(bad), encoding='utf-8')
            with self.subTest(key=key):
                self.assertIsNone(csp._partial_snapshot_load('fixture'))
        payload['schema'] = True
        path.write_text(json.dumps(payload), encoding='utf-8')
        self.assertIsNone(csp._partial_snapshot_load('fixture'))

    def test_online_partial_saved_and_failure_restores_it(self):
        def fetch(requests, *args):
            return {name: ((200, json.dumps({'sha': 'a'*40})) if isinstance(name, str)
                           else (200, 'public source fixture')) for name, _ in requests}
        with patch.object(csp, '_fetch_manifest_batch', side_effect=fetch), \
             patch.object(csp, '_parse_sources', side_effect=lambda *a, **kw: self.identity()):
            original = csp.extract_remote()
        self.assertFalse(original.stale)
        self.assertFalse(original.ok)
        self.assertEqual(len(list(Path(self.temp.name).glob('*.partial.json'))), 1)
        csp._remote_cache.clear()
        csp._partial_snapshot_cache.clear()
        with patch.object(csp, '_fetch_manifest_batch', return_value={}):
            restored = csp.extract_remote()
        self.assertTrue(restored.stale)
        self.assertEqual(restored.snapshot_id, original.snapshot_id)
        self.assertEqual(restored.expires_at, original.expires_at)
        with patch.object(csp.time, 'time', return_value=original.expires_at), \
             patch.object(csp, '_fetch_manifest_batch', return_value={}) as fetcher:
            expired = csp.extract_remote()
        self.assertTrue(fetcher.called)
        self.assertNotEqual(expired.snapshot_id, original.snapshot_id)
        self.assertFalse(expired.stale)

    def test_newer_partial_retains_priority_over_older_complete_snapshot(self):
        partial = self.identity()
        complete = copy.deepcopy(partial)
        complete.checked_at -= 10
        complete.expires_at = complete.checked_at + csp._REMOTE_TTL
        complete.coverage = {key: 'covered' for key in complete.coverage}
        complete.snapshot_id = 'c' * 64
        self.assertTrue(complete.ok)
        csp._last_good_save('fixture', complete)
        csp._partial_snapshot_save('fixture', partial)
        restored = csp._last_good_fallback('fixture', csp.CpaIdentity())
        self.assertEqual(restored.snapshot_id, partial.snapshot_id)
        self.assertFalse(restored.ok)
        self.assertEqual(restored.coverage, partial.coverage)


if __name__ == '__main__':
    unittest.main()
