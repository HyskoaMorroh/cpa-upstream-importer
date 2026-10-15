"""Temporary policy waits must preserve budgets, cancellation and wire limits."""
import concurrent.futures
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import client, probe_policy
from cpa_probe.pipeline import Prober, ProbePolicyStop
from cpa_probe import pipeline
from cpa_probe.probe_policy import Decision, Permit, ProbePolicy
from test_probe_policy_transport import response


class EvidenceWaitTests(unittest.TestCase):
    def test_cancelled_waiter_does_not_wait_for_the_probe_owner(self):
        from types import SimpleNamespace

        prober = object.__new__(Prober)
        prober.session_id = 'fixture-evidence-wait'
        prober._cancelled = threading.Event()
        prober._lock = threading.Lock()
        prober._shape = {}
        prober._dead_shape = {}
        prober.timeout = 60
        prober._policy_check = Mock()
        key = ('https://fixture.invalid', 'codex-api-key', 'fixture-evidence')
        prober._evidence_key = Mock(return_value=key)
        gate = Mock()

        def cancel_waiter(timeout):
            if prober._cancelled.is_set():
                self.fail('waited after cancellation')
            prober.cancel()
            return False

        gate.wait.side_effect = cancel_waiter
        prober._inflight = {key: gate}
        prober._full_probe = Mock()
        row = SimpleNamespace(bare='https://fixture.invalid', api_key='fixture-key')
        with self.assertRaises(concurrent.futures.CancelledError):
            prober._probe_one_section(row, 'codex-api-key')
        self.assertLessEqual(gate.wait.call_args.kwargs['timeout'], 0.25)
        self.assertIs(prober._inflight[key], gate)
        gate.set.assert_not_called()
        prober._full_probe.assert_not_called()


class PolicyWaitTests(unittest.TestCase):
    url = 'https://fixture.invalid/v1/models'
    headers = {'Authorization': 'Bearer fixture-key'}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = object.__new__(Prober)
        self.p.session_id = 'fixture-policy-wait'
        self.p._cancelled = threading.Event()
        self.p.policy_wait_timeout = 0.25
        self.p.timeout = 1
        self.p.gap = 0
        self.p._host_gap = {}
        self.p.on_event = Mock()
        self.engine_patch = patch.object(probe_policy, 'get_policy')
        self.get_policy = self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.client_patch = patch.object(client, 'get_policy', side_effect=lambda: self.get_policy())
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.opener_patch = patch.object(client, '_opener')
        self.open = self.opener_patch.start().return_value.open
        self.addCleanup(self.opener_patch.stop)
        self.open.return_value = response()

    def engine(self, interval=0):
        rule = {'enabled': True, 'provider': 'fixture', 'max_requests': 5,
                'max_requests_per_credential': 5, 'window_seconds': 3600,
                'min_interval_seconds': interval}
        engine = ProbePolicy({'version': 1, 'sites': {'fixture.invalid': rule}},
                             str(Path(self.temp.name) / 'ledger.sqlite3'))
        self.get_policy.return_value = engine
        return engine

    def send(self, method='POST', timeout=1):
        return self.p._send(self.url, headers=self.headers, body=b'{}',
                            method=method, timeout=timeout)

    def test_catalog_then_generation_waits_for_the_real_policy_interval(self):
        self.engine(interval=0.03)
        first = self.send('GET')
        second = self.send('POST')
        self.assertEqual('200', first.status)
        self.assertEqual('200', second.status)
        self.assertEqual(2, self.open.call_count)

    def test_provider_queue_starts_a_fresh_policy_wait_window(self):
        self.engine()
        clock = [10.0]
        @contextmanager
        def delayed_slot(*args):
            clock[0] = 100.0
            yield
        with patch.object(pipeline.PROVIDER_ADMISSION, 'slot', delayed_slot), \
                patch.object(pipeline.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.p._provider_admission(self.url, self.headers, 1) as (_, deadline):
                self.assertEqual(100.25, deadline)

    def test_send_does_not_reenter_provider_admission_via_policy_check(self):
        self.engine()
        self.p._policy_check = Mock(side_effect=AssertionError('recursive admission'))
        self.assertEqual('200', self.send().status)
        self.p._policy_check.assert_not_called()

    def test_expired_global_deadline_prevents_wire_send(self):
        self.engine()
        self.p.job_deadline = 0
        with self.assertRaises(ProbePolicyStop) as caught:
            self.send()
        self.assertEqual('policy_wait_timeout', caught.exception.code)
        self.open.assert_not_called()

    def test_host_wait_expiry_prevents_wire_send(self):
        self.engine()
        clock = [10.0]
        @contextmanager
        def delayed_host(*args):
            clock[0] = 11.0
            args[2]()
            yield
        with patch.object(pipeline.HOST_LIMITER, 'slot', delayed_host), \
                patch.object(pipeline.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ProbePolicyStop):
                self.send()
        self.open.assert_not_called()

    def test_provider_aliases_and_websocket_share_admission_key(self):
        rule = {'enabled': True, 'provider': 'fixture', 'max_requests': 5,
                'max_requests_per_credential': 5, 'window_seconds': 3600,
                'min_interval_seconds': 0}
        config = {'version': 1, 'sites': {'fixture.invalid': rule, 'alias.invalid': rule}}
        ledger = str(Path(self.temp.name) / 'shared.sqlite3')
        first, second = ProbePolicy(config, ledger), ProbePolicy(config, ledger)
        self.assertTrue(first.check(self.url, self.headers).allowed)
        self.assertTrue(second.check('wss://alias.invalid/v1/responses', self.headers).allowed)
        key = first.admission_key(self.url, self.headers)
        self.assertEqual(key, second.admission_key('wss://alias.invalid/v1/responses', self.headers))

    def test_inflight_wait_resumes_after_its_owner_finishes(self):
        engine = self.engine()
        permit = engine.reserve(self.url, self.headers)
        self.assertTrue(permit.allowed)
        self.p._wait = Mock(side_effect=lambda _seconds: engine.finish(permit, '200'))
        self.p._policy_check(self.url, self.headers)
        self.p._wait.assert_called_once()
        self.assertTrue(engine.check(self.url, self.headers).allowed)
        self.open.assert_not_called()

    def test_orphaned_reservation_times_out_without_releasing_the_ledger(self):
        engine = self.engine()
        permit = engine.reserve(self.url, self.headers)
        self.p.policy_wait_timeout = 0.015
        with self.assertRaises(ProbePolicyStop) as caught:
            self.p._policy_check(self.url, self.headers)
        self.assertEqual('policy_wait_timeout', caught.exception.code)
        self.assertEqual('inflight', engine.check(self.url, self.headers).code)
        self.assertIn(permit.reservation_id, engine._permits)
        self.open.assert_not_called()

    def test_cancellation_propagates_while_waiting(self):
        engine = self.engine()
        engine.reserve(self.url, self.headers)
        self.p._wait = Mock(side_effect=concurrent.futures.CancelledError())
        with self.assertRaises(concurrent.futures.CancelledError):
            self.p._policy_check(self.url, self.headers)
        self.open.assert_not_called()

    def test_hard_denials_are_not_retried(self):
        engine = Mock()
        engine.check.return_value = Decision(False, 'provider_stopped', 'fixture stop')
        self.get_policy.return_value = engine
        self.p._wait = Mock()
        with self.assertRaises(ProbePolicyStop) as caught:
            self.p._policy_check(self.url, self.headers)
        self.assertEqual('provider_stopped', caught.exception.code)
        self.p._wait.assert_not_called()
        self.open.assert_not_called()

    def race_policy(self):
        engine = Mock()
        engine.check.return_value = Decision(True, 'allowed', '')
        engine.finish.return_value = True
        self.get_policy.return_value = engine
        return engine

    def test_atomic_reservation_race_waits_without_sending_twice(self):
        engine = self.race_policy()
        engine.reserve.side_effect = [Permit(False, 'inflight', 'fixture busy'),
                                      Permit(True, 'reserved', '', 'fixture-permit')]
        self.p._wait = Mock()
        self.assertEqual('200', self.send().status)
        self.assertEqual(2, engine.reserve.call_count)
        self.assertEqual(1, self.open.call_count)
        engine.finish.assert_called_once()

    def test_queue_wait_does_not_consume_the_http_wire_deadline(self):
        engine = self.race_policy()
        engine.reserve.side_effect = [Permit(False, 'minimum_interval', 'fixture wait'),
                                      Permit(True, 'reserved', '', 'fixture-permit')]
        clock = [0.0]
        def wait_for_policy(_policy, _permit):
            clock[0] = 2.0
        with patch.object(client.time, 'monotonic', side_effect=lambda: clock[0]):
            result = client.send(self.url, headers=self.headers, body=b'{}',
                                 timeout=1, policy_wait=wait_for_policy)
        self.assertEqual('200', result.status)
        self.assertEqual(1, self.open.call_count)

    def test_reservation_wait_cancellation_does_not_become_a_ledger_error(self):
        engine = self.race_policy()
        engine.reserve.return_value = Permit(False, 'inflight', 'fixture busy')
        self.p._wait = Mock(side_effect=concurrent.futures.CancelledError())
        with self.assertRaises(concurrent.futures.CancelledError):
            self.send('POST')
        self.open.assert_not_called()
        engine.finish.assert_not_called()

    def test_sent_post_is_never_replayed(self):
        engine = self.race_policy()
        engine.reserve.return_value = Permit(True, 'reserved', '', 'fixture-permit')
        self.open.side_effect = ConnectionResetError('fixture reset')
        self.send('POST')
        self.assertEqual(1, self.open.call_count)
        self.assertEqual(1, engine.reserve.call_count)

    def test_get_retry_denial_does_not_restart_the_wire_attempt_budget(self):
        engine = self.race_policy()
        engine.reserve.side_effect = [Permit(True, 'reserved', '', 'fixture-permit'),
                                      Permit(False, 'minimum_interval', 'fixture wait')]
        self.open.side_effect = ConnectionResetError('fixture reset')
        self.p._wait = Mock()
        with self.assertRaises(ProbePolicyStop):
            self.send('GET')
        self.assertEqual(1, self.open.call_count)
        self.assertEqual(2, engine.reserve.call_count)
        self.p._wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
