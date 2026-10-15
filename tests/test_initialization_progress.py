"""Constructor progress tests: no network, real worker synchronization."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class InitializationProgressTests(unittest.TestCase):
    def test_wait_is_visible_without_calls_and_completion_advances_cursor(self):
        job = server.Job('fixture', [], {})
        job.state = 'running'
        entered, release = threading.Event(), threading.Event()
        sentinel = object()
        result = []
        def construct(**kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError('constructor was not released')
            return sentinel
        with patch.object(server, 'Prober', side_effect=construct) as constructor:
            worker = threading.Thread(target=lambda: result.append(
                server._create_prober_for_job(job, gap=0.5)))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                first = job.snapshot()
                self.assertEqual('initializing', first['phase'])
                self.assertEqual(0, first['calls'])
                self.assertTrue(first['phase_message'])
                with patch.object(server.time, 'time', return_value=job.started + 30):
                    later = job.snapshot(first['event_cursor'])
                self.assertEqual(30, later['elapsed'])
                self.assertEqual([], later['events'])
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([sentinel], result)
            constructor.assert_called_once_with(gap=0.5)
        final = job.snapshot(first['event_cursor'])
        self.assertEqual('probing', final['phase'])
        self.assertEqual(0, final['calls'])
        self.assertGreater(final['event_cursor'], first['event_cursor'])

    def test_sync_constructor_contract_is_unchanged(self):
        sentinel = object()
        with patch.object(server, 'Prober', return_value=sentinel) as constructor:
            self.assertIs(sentinel, server._create_prober_for_job(None, timeout=12))
        constructor.assert_called_once_with(timeout=12)

    def test_terminal_snapshot_clears_initialization_message(self):
        for state in ('done', 'error', 'cancelled'):
            with self.subTest(state=state):
                job = server.Job('fixture', [], {})
                job.emit('info', {'phase': 'initializing', 'msg': 'working'})
                job.state = state
                self.assertEqual(state, job.snapshot()['phase'])
                self.assertEqual('', job.snapshot()['phase_message'])

    def test_both_probe_workers_report_safe_constructor_failure(self):
        for runner in (server.run_job, server.run_job_full_redetect):
            with self.subTest(runner=runner.__name__), tempfile.TemporaryDirectory() as directory:
                cfg = Path(directory) / 'config.yaml'
                cfg.write_text('codex-api-key: []\n', encoding='utf-8')
                job = server.Job('fixture', [], {})
                with patch.object(server, 'Prober', side_effect=RuntimeError(
                        'https://private.fixture.invalid SECRET_FIXTURE')), patch.object(server, 'logger'):
                    runner(job, str(cfg))
                snapshot = job.snapshot()
                serialized = json.dumps(snapshot)
                self.assertEqual('error', snapshot['state'])
                self.assertEqual('error', snapshot['phase'])
                self.assertEqual(0, snapshot['calls'])
                self.assertNotIn('private.fixture', serialized)
                self.assertNotIn('SECRET_FIXTURE', serialized)
                self.assertTrue(any(e.get('phase') == 'initializing' for e in snapshot['events']))

    def test_async_diagnostic_returns_and_polls_during_constructor_wait(self):
        store = server.Store()
        handler = object.__new__(server.Handler)
        handler.headers = {}
        handler.client_address = ('127.0.0.1', 1234)
        handler.wfile = io.BytesIO()
        handler._load_cfg = lambda: ('codex-api-key: []', {})
        handler._json = lambda status, data: setattr(handler, 'response', (status, data))
        entered, release = threading.Event(), threading.Event()
        threads = []
        real_thread = threading.Thread
        def tracked_thread(**kwargs):
            thread = real_thread(**kwargs)
            threads.append(thread)
            return thread
        def construct(**kwargs):
            entered.set()
            release.wait(3)
            raise RuntimeError('https://private.fixture.invalid SECRET_FIXTURE')
        with patch.object(server, 'STORE', store), patch.object(server, 'logger') as logger, \
                patch.object(server, 'Prober', side_effect=construct), \
                patch.object(server.threading, 'Thread', side_effect=tracked_thread):
            try:
                handler._api_diag({'url': 'https://fixture.invalid', 'key': 'fixture-key', 'async': True})
                self.assertEqual(202, handler.response[0])
                job_id = handler.response[1]['job_id']
                self.assertTrue(entered.wait(2))
                handler._api_job(job_id, 0)
                self.assertEqual('initializing', handler.response[1]['phase'])
                self.assertEqual(0, handler.response[1]['calls'])
            finally:
                release.set()
                for thread in threads:
                    thread.join(3)
            handler._api_job(job_id, 0)
            self.assertEqual('error', handler.response[1]['state'])
            self.assertEqual('error', handler.response[1]['phase'])
            serialized = json.dumps(handler.response) + str(logger.mock_calls)
            self.assertNotIn('private.fixture', serialized)
            self.assertNotIn('SECRET_FIXTURE', serialized)


if __name__ == '__main__':
    unittest.main()
