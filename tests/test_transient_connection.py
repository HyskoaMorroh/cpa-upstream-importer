"""Retry safe reads only; an ambiguous POST may already have been executed."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import client


class TransientConnectionTests(unittest.TestCase):
    def _opener_raising(self, errors, final=None):
        """Return an opener whose open() raises each error in turn."""
        seq = list(errors)
        calls = []

        class _Resp:
            status = 403
            headers = {"Content-Type": "application/json"}

            def read(self, *a):
                return b'{"error":{"message":"fixture"}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def close(self):
                pass

        class _Opener:
            def open(_self, req, timeout=None):
                calls.append(getattr(req, 'full_url', req))
                if seq:
                    raise seq.pop(0)
                if final is not None:
                    raise final
                return _Resp()

        return _Opener(), calls

    def test_one_aborted_get_is_retried(self):
        aborted = ConnectionAbortedError(
            10053, 'An established connection was aborted by the software '
                   'in your host machine')
        opener, calls = self._opener_raising([aborted])
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'', method='GET', timeout=5)
        self.assertEqual(2, len(calls), 'the aborted attempt must be retried')
        self.assertNotEqual('000', resp.status,
                            f'retry should have succeeded, got {resp.error!r}')

    def test_a_reset_get_is_retried(self):
        opener, calls = self._opener_raising([ConnectionResetError(104, 'reset')])
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'', method='GET', timeout=5)
        self.assertEqual(2, len(calls))
        self.assertNotEqual('000', resp.status)

    def test_a_host_that_never_answers_still_reports_000(self):
        # Retrying must not hide a genuinely unreachable host, and must not
        # keep retrying: one extra attempt, then report the failure.
        err = ConnectionRefusedError(111, 'refused')
        opener, calls = self._opener_raising([err], final=err)
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'', method='GET', timeout=5)
        self.assertEqual('000', resp.status)
        self.assertLessEqual(len(calls), 2, 'at most one retry')

    def test_a_timeout_is_not_retried(self):
        # A timeout already consumed the caller's budget; retrying doubles the
        # wall clock on exactly the hosts that are slowest to begin with.
        opener, calls = self._opener_raising([TimeoutError('timed out')],
                                             final=TimeoutError('timed out'))
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'', method='GET', timeout=5)
        self.assertEqual('000', resp.status)
        self.assertEqual(1, len(calls), 'a timeout must not be retried')


    def test_post_is_not_replayed_after_ambiguous_open_failure(self):
        opener, calls = self._opener_raising([ConnectionResetError(104, 'reset')])
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/generate', headers={}, body=b'{}', timeout=5)
        self.assertEqual('000', resp.status)
        self.assertEqual(1, len(calls), 'the server may already have charged for the POST')

    def test_post_body_reset_never_replays_the_request(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.status = 200
        response.headers = {}
        response.__enter__.return_value = response
        response.read.side_effect = ConnectionResetError(104, 'body reset')
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/generate', headers={}, body=b'{}', timeout=5)
        self.assertEqual(1, opener.open.call_count)
        self.assertEqual('000', resp.status)

    def test_expired_retry_budget_returns_response_not_exception(self):
        import urllib.error
        for error in (ConnectionResetError(104, 'reset'),
                      urllib.error.URLError(ConnectionResetError(104, 'reset'))):
            with self.subTest(error=type(error).__name__):
                opener, calls = self._opener_raising([error])
                with patch.object(client, '_opener', return_value=opener),                         patch.object(client.time, 'monotonic', side_effect=[0, 0, 6, 6]):
                    resp = client.send('http://127.0.0.1:9/models', headers={}, body=b'', method='GET', timeout=5)
                self.assertEqual('000', resp.status)
                self.assertEqual(1, len(calls))


if __name__ == '__main__':
    unittest.main()
