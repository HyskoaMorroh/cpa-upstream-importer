"""A dropped connection is not an answer, so it must be retried once.

`client.send` turns every connection-layer failure into status `000`. That
value deliberately neither opens the breaker nor resets the status-code
streak, so one dropped connection makes the caller re-run a full probe for
that credential. On Windows loopback a short burst reliably produces
WinError 10053 (~3% of requests), and the same shape appears on real
upstreams behind a load balancer that recycles idle connections.

Retrying inside `send` keeps the cost at one extra request instead of a full
re-probe, and keeps a genuinely unreachable host reporting `000` as before.
"""
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

    def test_one_aborted_connection_is_retried(self):
        aborted = ConnectionAbortedError(
            10053, 'An established connection was aborted by the software '
                   'in your host machine')
        opener, calls = self._opener_raising([aborted])
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'{}', timeout=5)
        self.assertEqual(2, len(calls), 'the aborted attempt must be retried')
        self.assertNotEqual('000', resp.status,
                            f'retry should have succeeded, got {resp.error!r}')

    def test_a_reset_connection_is_retried(self):
        opener, calls = self._opener_raising([ConnectionResetError(104, 'reset')])
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'{}', timeout=5)
        self.assertEqual(2, len(calls))
        self.assertNotEqual('000', resp.status)

    def test_a_host_that_never_answers_still_reports_000(self):
        # Retrying must not hide a genuinely unreachable host, and must not
        # keep retrying: one extra attempt, then report the failure.
        err = ConnectionRefusedError(111, 'refused')
        opener, calls = self._opener_raising([err], final=err)
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'{}', timeout=5)
        self.assertEqual('000', resp.status)
        self.assertLessEqual(len(calls), 2, 'at most one retry')

    def test_a_timeout_is_not_retried(self):
        # A timeout already consumed the caller's budget; retrying doubles the
        # wall clock on exactly the hosts that are slowest to begin with.
        opener, calls = self._opener_raising([TimeoutError('timed out')],
                                             final=TimeoutError('timed out'))
        with patch.object(client, '_opener', return_value=opener):
            resp = client.send('http://127.0.0.1:9/v1/chat/completions',
                               headers={}, body=b'{}', timeout=5)
        self.assertEqual('000', resp.status)
        self.assertEqual(1, len(calls), 'a timeout must not be retried')


if __name__ == '__main__':
    unittest.main()
