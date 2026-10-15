"""Asynchronous diagnostic lifecycle tests, with no transport or real worker."""
from pathlib import Path
import io
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class AsyncDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.store = server.Store()
        for guard in (patch.object(server, 'STORE', self.store),
                      patch.object(server, 'logger'),
                      patch.object(server, 'Prober', side_effect=AssertionError('No real probe in this test'))):
            guard.start()
            self.addCleanup(guard.stop)
        self.h = object.__new__(server.Handler)
        self.h.headers = {}
        self.h.client_address = ('127.0.0.1', 1234)
        self.h.wfile = io.BytesIO()
        self.h._load_cfg = lambda: ('codex-api-key: []', {})
        self.h._json = lambda status, data: setattr(self.h, 'response', (status, data))
        self.body = {'url': 'https://fixture.invalid', 'key': 'fixture-diag-key',
                     'async': True, 'full': False}

    def test_start_returns_a_handle_before_the_diagnostic_runs(self):
        executed = []
        def diagnostic(worker, body):
            executed.append(body)
            worker._json(200, {'host': 'fixture.invalid', 'sections': {}, 'full': body['full']})
        with patch.object(server.Handler, '_diag_body', diagnostic, create=True), \
                patch.object(server.threading, 'Thread') as thread:
            self.h._api_diag(self.body)
            self.assertEqual(202, self.h.response[0])
            self.assertEqual([], executed)
            data = self.h.response[1]
            self.assertEqual('diag', data['kind'])
            self.assertTrue(data['boot_id'])
            self.assertIn(data['job_id'], self.store.jobs)
            thread.call_args.kwargs['target']()
        self.h._api_job(data['job_id'], 0)
        self.assertEqual('done', self.h.response[1]['state'])
        self.assertEqual('fixture.invalid', self.h.response[1]['result']['host'])
        self.assertIs(False, self.h.response[1]['result']['full'])
        self.assertEqual(1, len(executed))

    def test_legacy_sync_diagnostic_keeps_its_original_response(self):
        expected = {'host': 'fixture.invalid', 'sections': {}, 'full': False}
        def diagnostic(worker, body):
            worker._json(200, expected)
        body = dict(self.body)
        del body['async']
        with patch.object(server.Handler, '_diag_body', diagnostic, create=True):
            self.h._api_diag(body)
        self.assertEqual((200, expected), self.h.response)
        self.assertFalse(self.store.jobs)

    def test_async_flag_must_be_a_boolean(self):
        with self.assertRaises(ValueError), patch.object(server.threading, 'Thread') as thread:
            self.h._api_diag({**self.body, 'async': 'true'})
        thread.assert_not_called()

    def test_thread_start_failure_returns_a_terminal_job(self):
        with patch.object(server.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('fixture thread failure')
            self.h._api_diag(self.body)
        self.assertEqual(503, self.h.response[0])
        job = self.store.jobs[self.h.response[1]['job_id']]
        self.assertEqual('error', job.state)
        self.assertGreater(job.finished, 0)

    def test_diagnostic_job_cannot_be_used_as_a_write_plan(self):
        with patch.object(server.threading, 'Thread'):
            self.h._api_diag(self.body)
        job_id = self.h.response[1]['job_id']
        self.store.jobs[job_id].state = 'done'
        with patch.object(self.h, '_plan_body', side_effect=AssertionError('Not a write job')):
            self.h._api_plan({'job_id': job_id})
        self.assertEqual(409, self.h.response[0])

    def test_worker_exception_is_redacted_in_job_and_poll_response(self):
        def diagnostic(worker, body):
            raise RuntimeError('fixture failure ' + body['key'])
        with patch.object(server.Handler, '_diag_body', diagnostic, create=True), \
                patch.object(server.threading, 'Thread') as thread:
            self.h._api_diag(self.body)
            job_id = self.h.response[1]['job_id']
            thread.call_args.kwargs['target']()
        job = self.store.jobs[job_id]
        self.assertEqual('error', job.state)
        self.assertNotIn(self.body['key'], job.error)
        self.h._api_job(job_id, 0)
        self.assertNotIn(self.body['key'], repr(self.h.response))


if __name__ == '__main__':
    unittest.main()
