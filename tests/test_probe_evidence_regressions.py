"""Probe evidence and cancellation regression cases without network transport."""
import concurrent.futures
import copy
from pathlib import Path
import json
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import parse_lines
from cpa_probe.fingerprint import model_matches, resp_model
from cpa_probe.pipeline import Attempt, Prober, SectionVerdict


class ResponseModelTests(unittest.TestCase):
    def test_gemini_stream_keeps_its_model_version(self):
        body = 'data: ' + json.dumps({'modelVersion': 'gemini-4-flash'}) + '\n\n'
        self.assertEqual('gemini-4-flash', resp_model(body))
        self.assertFalse(model_matches('gemini-4-pro', resp_model(body)))

    def test_existing_model_field_parsing_is_unchanged(self):
        body = 'data: ' + json.dumps({'model': 'gpt-6-astra'}) + '\n\ndata: [DONE]\n\n'
        self.assertEqual('gpt-6-astra', resp_model(body))

    def test_absent_model_is_unknown_not_a_claimed_mismatch(self):
        self.assertIsNone(resp_model('data: {"content":"fixture"}\n\n'))
        self.assertTrue(model_matches('gpt-6-astra', None))


class AcceptedModelTests(unittest.TestCase):
    def make_prober(self, discover=False):
        p = object.__new__(Prober)
        p.session_id = "fixture-job"
        p._policy_local = threading.local()
        v = SectionVerdict('codex-api-key', usable=True, models=[], category='可用')
        p._stage1 = Mock(return_value=v)
        p._stage2 = Mock(side_effect=(lambda _row, verdict: verdict.models.append('gpt-6-astra'))
                         if discover else None)
        p._stage4_swap = Mock()
        p._stage4_context = Mock()
        p._stage5_capabilities = Mock()
        return p

    def test_no_accepted_model_is_not_usable(self):
        p = self.make_prober()
        row = parse_lines('https://fixture.invalid,fixture-key', allow_private=True).valid[0]
        v = p._full_probe(row, 'codex-api-key')
        self.assertFalse(v.usable)
        self.assertNotEqual('可用', v.category)
        p._stage4_context.assert_not_called()
        p._stage5_capabilities.assert_not_called()

    def test_model_scan_can_recover_from_a_rejected_baseline_model(self):
        p = self.make_prober(discover=True)
        row = parse_lines('https://fixture.invalid,fixture-key', allow_private=True).valid[0]
        v = p._full_probe(row, 'codex-api-key')
        self.assertTrue(v.usable)
        self.assertEqual(['gpt-6-astra'], v.models)
        p._stage5_capabilities.assert_called_once()


class CancelPropagationTests(unittest.TestCase):
    def test_parallel_section_cancellation_is_not_a_dead_route(self):
        p = object.__new__(Prober)
        p.workers = 2
        p.on_event = Mock()
        p.cancel = Mock()
        p._probe_one_section = Mock(side_effect=concurrent.futures.CancelledError())
        row = parse_lines('https://fixture.invalid,fixture-key', allow_private=True).valid[0]
        with self.assertRaises(concurrent.futures.CancelledError):
            p.probe(row)
        p.cancel.assert_called_once()


class CatalogRequestShapeTests(unittest.TestCase):
    def probe_catalog(self, key):
        p = object.__new__(Prober)
        p.cfg_snapshot = {'codex-api-key': [{
            'api-key': 'fixture-key', 'base-url': 'https://fixture.invalid/channel/v1',
            'proxy-url': 'http://proxy.invalid:7890',
            'headers': {'X-Fixture-Required': 'fixture-gate'},
        }]}
        p.client_headers = {}
        p.session_id = 'fixture-session'
        p.timeout = 1
        p._throttle = Mock()
        p.on_event = Mock()
        p._send = Mock(return_value=SimpleNamespace(
            status='200', body=json.dumps({'data': [{'id': 'gpt-6-astra'}]})))
        row = parse_lines('https://fixture.invalid/channel,' + key, allow_private=True).valid[0]
        self.assertEqual(['gpt-6-astra'], p._stage0_catalog(
            row, 'codex-api-key', row.base_for('codex-api-key')))
        return p._send.call_args

    def test_catalog_uses_the_entry_proxy_and_required_headers(self):
        sent = self.probe_catalog('fixture-key')
        self.assertEqual('http://proxy.invalid:7890', sent.kwargs['proxy'])
        headers = {k.lower(): v for k, v in sent.kwargs['headers'].items()}
        self.assertEqual('fixture-gate', headers['x-fixture-required'])
        self.assertEqual('GET', sent.kwargs['method'])
        self.assertEqual('https://fixture.invalid/channel/v1/models', sent.args[0])

    def test_other_credentials_do_not_inherit_entry_headers_or_proxy(self):
        sent = self.probe_catalog('another-fixture-key')
        self.assertIsNone(sent.kwargs['proxy'])
        self.assertNotIn('x-fixture-required', {k.lower() for k in sent.kwargs['headers']})


class EvidenceCacheIdentityTests(unittest.TestCase):
    def prober(self):
        p = object.__new__(Prober)
        p.max_models = 4
        p.max_model_attempts = 8
        p.cfg_snapshot = {}
        p.proxy = None
        p.client_headers = {}
        p.session_id = 'fixture-session'
        p.timeout = 1
        p._lock = threading.RLock()
        p._shape = {}
        p._dead_shape = {}
        p._fail_streak = {}
        p._inflight = {}
        p._policy_check = Mock()
        p._check_cancel = Mock()
        p.on_event = Mock()
        p._full_probe = Mock(side_effect=lambda row, sec: SectionVerdict(
            sec, usable=True, models=['gpt-6-astra' if row.api_key == 'key-a' else 'gpt-6-sol']))
        p._reuse_shape = Mock(side_effect=lambda row, sec, shape: copy.deepcopy(shape))
        p._reuse_dead = Mock(side_effect=lambda row, sec, shape: copy.deepcopy(shape))
        return p

    def row(self, key):
        return parse_lines('https://fixture.invalid/channel,' + key, allow_private=True).valid[0]

    def test_different_credentials_do_not_share_model_evidence(self):
        p = self.prober()
        p._probe_one_section(self.row('key-a'), 'codex-api-key')
        second = p._probe_one_section(self.row('key-b'), 'codex-api-key')
        self.assertEqual(['gpt-6-sol'], second.models)
        self.assertEqual(2, p._full_probe.call_count)
        p._reuse_shape.assert_not_called()

    def test_identical_credential_and_request_context_can_still_reuse(self):
        p = self.prober()
        row = self.row('key-a')
        p._probe_one_section(row, 'codex-api-key')
        p._probe_one_section(row, 'codex-api-key')
        self.assertEqual(1, p._full_probe.call_count)
        p._reuse_shape.assert_called_once()

    def test_real_same_context_reuse_keeps_identity_provenance(self):
        p = self.prober()
        del p._reuse_shape
        # This unit checks cached model provenance. Family continuation is
        # exercised separately by test_family_model_sweep and integration tests.
        p._stage2 = Mock()
        shape = SectionVerdict('codex-api-key', usable=True,
                               models=['gpt-6-astra', 'gpt-6-sol'],
                               min_headers={'User-Agent': 'fixture-client'},
                               source_snapshot_id='fixture-snapshot',
                               identity_verified_models=['gpt-6-astra', 'gpt-6-sol'])
        p._profile_kwargs = Mock(return_value={'extra_headers': shape.min_headers})
        p._call = Mock(side_effect=lambda section, base, key, model, **kwargs: Attempt(
            section=section, model=model, combo='reuse-verify',
            status='200', category='可用', action='', elapsed_ms=1,
            resp_model=model, identity_verified=True, response_valid=True,
            successful_base_url=base))
        result = p._reuse_shape(self.row('key-a'), 'codex-api-key', shape)
        self.assertTrue(result.usable)
        self.assertTrue(result.need_ua)
        self.assertEqual('fixture-snapshot', result.source_snapshot_id)
        self.assertEqual(shape.identity_verified_models, result.identity_verified_models)
        self.assertEqual(2, p._call.call_count)
        p._stage2.assert_called_once()

    def test_changed_client_headers_invalidate_the_evidence_cache(self):
        p = self.prober()
        row = self.row('key-a')
        p._probe_one_section(row, 'codex-api-key')
        p.client_headers = {'X-Fixture-Lane': 'another'}
        p._probe_one_section(row, 'codex-api-key')
        self.assertEqual(2, p._full_probe.call_count)

    def test_changed_proxy_invalidates_the_evidence_cache(self):
        p = self.prober()
        row = self.row('key-a')
        p._probe_one_section(row, 'codex-api-key')
        p.proxy = 'http://proxy.invalid:7890'
        p._probe_one_section(row, 'codex-api-key')
        self.assertEqual(2, p._full_probe.call_count)

    def test_one_credential_failure_does_not_skip_another_credential(self):
        p = self.prober()
        p._full_probe.side_effect = [SectionVerdict('codex-api-key', category='门禁'),
                                    SectionVerdict('codex-api-key', usable=True, models=['gpt-6-sol'])]
        p._probe_one_section(self.row('key-a'), 'codex-api-key')
        second = p._probe_one_section(self.row('key-b'), 'codex-api-key')
        self.assertTrue(second.usable)
        p._reuse_dead.assert_not_called()


if __name__ == '__main__':
    unittest.main()
