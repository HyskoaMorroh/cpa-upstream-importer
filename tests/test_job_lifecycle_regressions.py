"""Synthetic regressions for task termination, redaction and bounded HTTP reads."""
from pathlib import Path
import io
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class ReadTracker(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class ChunkedBodyTests(unittest.TestCase):
    def handler(self, data, limit=8):
        h = object.__new__(server.Handler)
        h.rfile = ReadTracker(data)
        h._MAX_BODY = limit
        return h

    def test_negative_chunk_is_rejected_before_any_body_read(self):
        h = self.handler(b'-1\r\n{"x":1}\r\n0\r\n\r\n')
        with self.assertRaises(ValueError):
            h._read_chunked()
        self.assertEqual([], h.rfile.read_sizes)

    def test_non_hex_size_syntax_is_rejected_before_any_body_read(self):
        for size in (b'+1', b'0x1', b'1_0'):
            with self.subTest(size=size):
                h = self.handler(size + b'\r\nx\r\n0\r\n\r\n', limit=32)
                with self.assertRaises(ValueError):
                    h._read_chunked()
                self.assertEqual([], h.rfile.read_sizes)

    def test_chunk_over_remaining_limit_is_rejected_before_read(self):
        h = self.handler(b'9\r\n123456789\r\n0\r\n\r\n')
        with self.assertRaises(ValueError):
            h._read_chunked()
        self.assertEqual([], h.rfile.read_sizes)

    def test_chunk_payload_requires_its_crlf(self):
        h = self.handler(b'1\r\nx!!0\r\n\r\n')
        with self.assertRaises(ValueError):
            h._read_chunked()

    def test_final_trailer_terminator_is_required(self):
        h = self.handler(b'1\r\nx\r\n0\r\n')
        with self.assertRaises(ValueError):
            h._read_chunked()

    def test_valid_extensions_and_trailers_still_work(self):
        h = self.handler(b'2;fixture=yes\r\n{}\r\n0\r\nX-Fixture: yes\r\n\r\n', limit=128)
        self.assertEqual(b'{}', h._read_chunked())

    def test_truncated_payload_is_rejected(self):
        h = self.handler(b'4\r\nxx')
        with self.assertRaises(ValueError):
            h._read_chunked()


class PlanLifecycleTests(unittest.TestCase):
    secret = 'fixture-sensitive-header-value'

    def setUp(self):
        self.log_patch = patch.object(server, 'logger')
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.store = server.Store()
        self.job = server.Job('fixture-job', [], {})
        self.job.state = 'done'
        self.store.add_job(self.job)
        self.store_patch = patch.object(server, 'STORE', self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.cache_patch = patch.object(server.Handler, '_plan_cache', {})
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def handler(self):
        h = object.__new__(server.Handler)
        h.headers = {}
        h.client_address = ('127.0.0.1', 1234)
        h.wfile = io.BytesIO()
        config = {'codex-api-key': [{'api-key': 'fixture-key',
                   'base-url': 'https://fixture.invalid/v1',
                   'headers': {'X-Fixture-Private': self.secret}}]}
        h._load_cfg = lambda: ('codex-api-key: []', config)
        h._json = lambda code, data: setattr(h, 'response', (code, data))
        return h

    def test_thread_start_failure_is_a_terminal_task(self):
        h = self.handler()
        with patch.object(server.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('fixture thread start failure')
            h._api_plan({'job_id': self.job.id})
        self.assertEqual(503, h.response[0])
        self.assertTrue(self.store.plan_tasks)
        for task in self.store.plan_tasks.values():
            self.assertEqual('error', task.state)
            self.assertGreater(task.finished, 0)

    def test_repeated_start_failure_does_not_exhaust_running_capacity(self):
        with patch.object(server.threading, 'Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('fixture thread start failure')
            for _ in range(self.store.MAX_PLAN_TASKS + 1):
                h = self.handler()
                h._api_plan({'job_id': self.job.id})
                self.assertEqual(503, h.response[0])
        self.assertFalse(any(t.state == 'running' for t in self.store.plan_tasks.values()))

    def test_cancelled_job_returns_its_completed_rows(self):
        from cpa_probe import parse_lines
        from cpa_probe.pipeline import CandidateResult
        row = parse_lines('https://fixture.invalid,fixture-key', allow_private=True).valid[0]
        self.job.results = [CandidateResult(row=row)]
        self.job.state = 'cancelled'
        h = self.handler()
        h._api_job(self.job.id, 0)
        self.assertEqual(200, h.response[0])
        self.assertEqual('cancelled', h.response[1]['state'])
        self.assertEqual(1, len(h.response[1]['results']))

    def test_cancelled_job_without_results_returns_an_empty_list(self):
        self.job.state = 'cancelled'
        h = self.handler()
        h._api_job(self.job.id, 0)
        self.assertEqual([], h.response[1].get('results'))

    def test_worker_error_is_redacted_before_storage_polling_and_logging(self):
        h = self.handler()
        def fail(*args, **kwargs):
            raise RuntimeError('fixture diagnostic includes ' + self.secret)
        h._plan_async_body = fail
        with patch.object(server.threading, 'Thread') as thread, \
                patch.object(server, 'logger') as log:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs['target']()
            h._api_plan({'job_id': self.job.id})
            task = next(iter(self.store.plan_tasks.values()))
            self.assertEqual('error', task.state)
            self.assertTrue(self.store._plan_task_busy(task), 'real worker results retain their polling window')
            self.assertNotIn(self.secret, task.error)
            h._api_plan_status(task.id)
            self.assertNotIn(self.secret, json.dumps(h.response))
            self.assertNotIn(self.secret, repr(log.mock_calls))
            self.assertFalse(log.exception.called, 'raw traceback logging can expose request secrets')


if __name__ == '__main__':
    unittest.main()
