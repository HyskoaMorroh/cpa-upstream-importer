"""Passive analysis must not send upstream requests or invent verification."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpa_probe as cp
from cpa_probe import client, cpa_source_probe as csp, probe_policy
from cpa_probe.pipeline import Prober
from cpa_probe.plan import SectionPlan
from cpa_probe.writeback import verify_upstream

class PassiveFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            'IMPORTER_PROBE_MODE': 'passive',
            'IMPORTER_PROBE_POLICY_FILE': str(Path(self.temp.name) / 'no-policy.json'),
            'IMPORTER_BACKUP_DIR': self.temp.name,
            'IMPORTER_PROBE_LEDGER': str(Path(self.temp.name) / 'ledger.sqlite3'),
        })
        env.start()
        self.addCleanup(env.stop)
        probe_policy.reset_policy_cache()
        self.addCleanup(probe_policy.reset_policy_cache)

    def test_all_sections_are_passive_without_upstream_sockets(self):
        row = cp.parse_lines('https://fixture.invalid,synthetic-credential', allow_private=True).valid[0]
        with patch.object(csp, 'cached_identity', return_value=csp.CpaIdentity()), \
                patch.object(client, '_opener', side_effect=AssertionError('network not permitted')) as opener:
            prober = Prober(gap=0, timeout=1, workers=4)
            result = prober.probe(row)
        opener.assert_not_called()
        self.assertEqual(0, result.total_calls)
        self.assertEqual(set(cp.SECTIONS), set(result.sections))
        for verdict in result.sections.values():
            self.assertFalse(verdict.usable)
            self.assertEqual('未实测', verdict.category)
            self.assertTrue(verdict.probe_policy_code)
            self.assertTrue(verdict.probe_policy_reason)
            self.assertFalse(verdict.attempts)

    def test_passive_plan_does_not_default_recommend_activation(self):
        plan = SectionPlan('codex-api-key', 'https://fixture.invalid', 'synthetic-key',
                           models=['gpt-6-fixture'], priority=1, model_source='seed',
                           probe_policy_code='passive', probe_policy_reason='未授权主动探测')
        self.assertFalse(plan.recommended)
        self.assertIn('未实测', plan.recommend_reason)
        self.assertFalse(plan.reenable_fields)

    def permitted_engine(self, **overrides):
        rule = {"enabled": True, "provider": "fixture", "max_requests": 5,
                "max_requests_per_credential": 5, "window_seconds": 3600,
                "min_interval_seconds": 0}
        rule.update(overrides)
        return probe_policy.ProbePolicy({"version": 1, "sites": {"fixture.invalid": rule}},
                                        str(Path(self.temp.name) / "active.sqlite3"))

    def test_client_restriction_stops_before_another_profile_or_protocol(self):
        from unittest.mock import MagicMock
        engine = self.permitted_engine()
        row = cp.parse_lines('https://fixture.invalid,synthetic-credential', allow_private=True).valid[0]
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 401
        response.length = None
        response.headers = {}
        response.read.return_value = b"only official clients are allowed"
        with patch.object(csp, 'cached_identity', return_value=csp.CpaIdentity()),                 patch.object(probe_policy, 'get_policy', return_value=engine),                 patch.object(client, 'get_policy', return_value=engine),                 patch.object(client, '_opener') as opener:
            opener.return_value.open.return_value = response
            result = Prober(gap=0, timeout=1, workers=1).probe(row)
        self.assertEqual(1, opener.return_value.open.call_count)
        self.assertTrue(all(v.probe_policy_code == 'provider_stopped' for v in result.sections.values()))
        self.assertTrue(all(not v.usable for v in result.sections.values()))

    def test_budget_stop_preserves_catalogue_already_obtained(self):
        from unittest.mock import MagicMock
        engine = self.permitted_engine(max_requests=1, max_requests_per_credential=1)
        row = cp.parse_lines('https://fixture.invalid,synthetic-credential', allow_private=True).valid[0]
        response = MagicMock()
        response.__enter__.return_value = response
        response.status, response.length, response.headers = 200, None, {}
        response.read.return_value = b'{"data":[{"id":"gpt-6"}]}'
        with patch.object(csp, 'cached_identity', return_value=csp.CpaIdentity()),                 patch.object(probe_policy, 'get_policy', return_value=engine),                 patch.object(client, 'get_policy', return_value=engine),                 patch.object(client, '_opener') as opener:
            opener.return_value.open.return_value = response
            verdict = Prober(gap=0, timeout=1, workers=1)._probe_one_section(row, 'codex-api-key')
        self.assertEqual(1, opener.return_value.open.call_count)
        self.assertEqual(['gpt-6'], verdict.catalog)
        self.assertFalse(verdict.usable)
        self.assertTrue(verdict.probe_policy_code)

    def test_final_websocket_bare_403_job_stop_reaches_the_plan(self):
        from unittest.mock import MagicMock
        from cpa_probe.pipeline import SectionVerdict, CandidateResult
        from cpa_probe import model_catalog
        engine = self.permitted_engine()
        row = cp.parse_lines('https://fixture.invalid,synthetic-credential', allow_private=True).valid[0]
        prior = SectionVerdict('codex-api-key', base_url=row.base_for('codex-api-key'),
                               usable=True, models=['gpt-6'], category='可用')
        sock = MagicMock()
        crlf = bytes((13, 10))
        # A bare 403 stops this job; only explicit restrictions are permanent.
        sock.recv.side_effect = [b'HTTP/1.1 403 Forbidden' + crlf + b'Content-Length: 0' + crlf + crlf]
        with patch.object(csp, 'cached_identity', return_value=csp.CpaIdentity()),                 patch.object(probe_policy, 'get_policy', return_value=engine),                 patch.object(client, 'get_policy', return_value=engine),                 patch.object(client.socket, 'create_connection', return_value=sock) as connect,                 patch.object(client.ssl, 'create_default_context') as tls:
            tls.return_value.wrap_socket.return_value = sock
            prober = Prober(gap=0, timeout=1, workers=1)
            with patch.object(prober, '_stage1', return_value=prior),                     patch.object(prober, '_stage2'), patch.object(prober, '_stage4_swap'),                     patch.object(prober, '_stage4_context'):
                verdict = prober._full_probe(row, 'codex-api-key')
            connect.assert_called_once()
            self.assertEqual('provider_job_stopped', verdict.probe_policy_code)
            result = CandidateResult(row=row)
            result.sections = {sec: SectionVerdict(sec) for sec in cp.SECTIONS}
            result.sections['codex-api-key'] = verdict
            with patch.object(model_catalog, 'remote_names', return_value=(['gpt-6'], '')):
                plan = cp.build_plan(row, result, {}, bands={}, seen=cp.existing_fingerprints({}), probation=True)
        section = plan.sections['codex-api-key']
        self.assertEqual('provider_job_stopped', section.probe_policy_code)
        self.assertFalse(section.recommended)
        self.assertFalse(section.reenable_fields)

    def test_cli_write_cannot_enable_a_new_untested_entry(self):
        from types import SimpleNamespace
        from cpa_probe.writeback import mark_new_sections
        section = SectionPlan('codex-api-key', 'https://fixture.invalid', 'synthetic-key',
                              models=['gpt-6-fixture'], priority=1, model_source='catalog',
                              probe_policy_code='passive', probe_policy_reason='未授权主动探测')
        blocked = mark_new_sections({}, [SimpleNamespace(sections={'codex-api-key': section})])
        self.assertEqual(1, blocked)
        self.assertFalse(section.writable)
        self.assertTrue(section.write_blocked)

    def test_gateway_verification_without_bound_upstream_is_skipped(self):
        scope = {}
        with patch.object(client, 'send', side_effect=AssertionError('gateway may forward to banned provider')) as send:
            ok, reason = verify_upstream('https://gateway.invalid', 'synthetic-gateway-key',
                                         'codex-api-key', 'gpt-6-fixture', scope=scope)
        send.assert_not_called()
        self.assertFalse(ok)
        self.assertEqual('skipped_policy', scope['verification_status'])
        self.assertIn('未实测', reason)
        self.assertFalse(scope['target_verified'])

class PassiveModelPreservationTests(unittest.TestCase):
    """Without probe evidence, an existing model list is the best record we have.

    Rewriting it from市面 guesses silently replaces a list the operator built
    from earlier real probes. Entries stay usable either way, so the damage is
    invisible until a request routes to a model the site never offered.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            'IMPORTER_PROBE_MODE': 'passive',
            'IMPORTER_PROBE_POLICY_FILE': str(Path(self.temp.name) / 'absent.json'),
            'IMPORTER_PROBE_LEDGER': str(Path(self.temp.name) / 'ledger.sqlite3'),
        })
        env.start()
        self.addCleanup(env.stop)
        probe_policy.reset_policy_cache()
        self.addCleanup(probe_policy.reset_policy_cache)

    def test_passive_plan_keeps_the_configured_model_list(self):
        import yaml
        from cpa_probe.pipeline import SectionVerdict, CandidateResult
        existing = yaml.safe_load("""
codex-api-key:
  - api-key: "synthetic-key"
    base-url: "https://fixture.invalid/v1"
    models:
      - name: "gpt-6-astra"
        alias: ""
      - name: "gpt-6-sol"
        alias: ""
      - name: "gpt-5.6-terra"
        alias: ""
""")
        row = cp.parse_lines('https://fixture.invalid,synthetic-key', allow_private=True).valid[0]
        result = CandidateResult(row=row)
        result.sections = {sec: SectionVerdict(sec) for sec in cp.SECTIONS}
        for sec, verdict in result.sections.items():
            verdict.base_url = row.base_for(sec)
            verdict.category = '未实测'
            verdict.probe_policy_code = 'passive'
            verdict.probe_policy_reason = '未授权主动探测'
        plan = cp.build_plan(row, result, existing, bands={},
                             seen=cp.existing_fingerprints(existing), probation=True)
        section = plan.sections['codex-api-key']
        self.assertEqual(['gpt-6-astra', 'gpt-6-sol', 'gpt-5.6-terra'], section.models,
                         'a passive plan must carry the configured list through unchanged')
        self.assertEqual('prior', section.model_source)


class PublicDefaultsTests(unittest.TestCase):
    """Shipped defaults must not carry one deployment's naming."""

    def test_compat_fallback_prefix_is_generic(self):
        from cpa_probe import prefixes
        fallback = prefixes.SECTION_PREFIX_FALLBACK['openai-compatibility']
        self.assertEqual('OAI', fallback)
        self.assertTrue(fallback.isascii() and fallback.isupper())

    def test_existing_configured_prefix_still_wins(self):
        from cpa_probe import prefixes
        assigned = prefixes.assign(['site-a.invalid'],
                                   existing={'site-a.invalid': 'KEEPME'})
        self.assertEqual('KEEPME', assigned['site-a.invalid'])


if __name__ == '__main__':
    unittest.main()
