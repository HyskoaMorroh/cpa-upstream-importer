"""Keep the UI responsive without discarding evidence used by probe jobs."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import cpa_source_probe as csp
from cpa_probe import model_catalog as mc
from cpa_probe.pipeline import Prober


class ProbeEvidenceTests(unittest.TestCase):
    def test_prober_freezes_the_completed_identity_for_this_job(self):
        identity = csp.CpaIdentity()
        identity.snapshot_id = 'completed-fixture'
        with patch.object(csp, 'cached_identity', return_value=identity) as obtain:
            prober = Prober(gap=0, timeout=1, probe_context=False,
                            probe_capabilities=False, swap_samples=0)
        self.assertEqual('completed-fixture', prober.source_identity.snapshot_id)
        obtain.assert_called_once()
        identity.snapshot_id = 'later-refresh'
        self.assertEqual('completed-fixture', prober.source_identity.snapshot_id)


class FirstPaintLatencyTests(unittest.TestCase):
    def setUp(self):
        mc.reset_remote_nonblocking()
        self.addCleanup(mc.reset_remote_nonblocking)
        self.now = [1000.]
        self.jobs = []
        clock = patch.object(mc.time, 'time', side_effect=lambda: self.now[0])
        clock.start()
        self.addCleanup(clock.stop)
        jobs = self.jobs
        class DeferredThread:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args
            def start(self):
                jobs.append((self.target, self.args))
        threads = patch.object(mc.threading, 'Thread', DeferredThread)
        threads.start()
        self.addCleanup(threads.stop)

    def finish(self, result):
        target, args = self.jobs.pop(0)
        with patch.object(mc, 'remote_names', return_value=result):
            target(*args)

    def test_first_paint_returns_pending_without_waiting_for_network(self):
        with patch.object(mc, 'remote_names') as network:
            names, why = mc.remote_names_nonblocking()
        self.assertEqual([], names)
        self.assertTrue(why)
        network.assert_not_called()
        self.assertEqual(1, len(self.jobs))

    def test_proxy_changes_do_not_reuse_another_request_cache(self):
        mc.remote_names_nonblocking(proxy='http://first.invalid:7890')
        self.finish((['gpt-fixture'], ''))
        self.assertEqual(['gpt-fixture'], mc.remote_names_nonblocking(proxy='http://first.invalid:7890')[0])
        self.assertEqual([], mc.remote_names_nonblocking(proxy='http://second.invalid:7890')[0])
        self.assertEqual(1, len(self.jobs))

    def test_nonempty_disk_fallback_has_failure_ttl(self):
        mc.remote_names_nonblocking()
        self.finish((['gpt-fixture'], 'using a previous disk snapshot'))
        self.now[0] += 601
        names, reason = mc.remote_names_nonblocking()
        self.assertEqual(['gpt-fixture'], names)
        self.assertTrue(reason)
        self.assertEqual(1, len(self.jobs), 'stale disk data must refresh after ten minutes')

    def test_reset_drops_late_completion_from_previous_generation(self):
        mc.remote_names_nonblocking()
        mc.reset_remote_nonblocking()
        mc.remote_names_nonblocking()
        self.finish((['old-fixture'], ''))
        self.assertEqual([], mc.remote_names_nonblocking()[0])
        self.finish((['new-fixture'], ''))
        self.assertEqual(['new-fixture'], mc.remote_names_nonblocking()[0])

    def test_thread_start_failure_does_not_stick_inflight(self):
        with patch.object(mc.threading, 'Thread', side_effect=RuntimeError('thread unavailable')):
            names, why = mc.remote_names_nonblocking()
        self.assertEqual([], names)
        self.assertTrue(why)
        mc.remote_names_nonblocking()
        self.assertEqual(1, len(self.jobs))

    def test_explicit_no_cache_never_returns_cached_names(self):
        mc.remote_names_nonblocking()
        self.finish((['gpt-fixture'], ''))
        names, why = mc.remote_names_nonblocking(use_cache=False)
        self.assertEqual([], names)
        self.assertTrue(why)

    def test_callers_cannot_mutate_stored_names(self):
        mc.remote_names_nonblocking()
        self.finish((['gpt-fixture'], ''))
        names, _ = mc.remote_names_nonblocking()
        names.append('caller-added')
        self.assertEqual(['gpt-fixture'], mc.remote_names_nonblocking()[0])

    def test_first_paint_states_why_the_market_signal_is_missing(self):
        import server
        top, by_line, why = server._market_top_gen({})
        self.assertEqual(({}, {}), (top, by_line))
        self.assertTrue(why)

    def test_nonempty_old_catalog_preserves_age_without_remote_details(self):
        import server
        reason = ('远程名录拉不通（https://private.example/?token=secret），'
                  '已改用落盘的上次成功名录（8 天前，1 个模型）；'
                  '该名录已超过 7 天，请尽快恢复出网或配置代理')
        with patch.object(mc, 'remote_names_nonblocking', return_value=(['gpt-99'], reason)), \
                patch.object(mc, 'latest_models', return_value=(['gpt-99'], 'fixture')):
            top, by_line, why = server._market_top_gen({})
        self.assertTrue(top)
        self.assertTrue(by_line)
        self.assertIn('8 天前', why)
        self.assertIn('超过 7 天', why)
        self.assertNotIn('private.example', why)
        self.assertNotIn('secret', why)

    def test_catalog_reason_never_exposes_arbitrary_remote_error(self):
        import server
        for reason in ('token=secret https://private.example', '后台 secret'):
            why = server._catalog_public_reason(reason)
            self.assertTrue(why)
            self.assertNotIn('secret', why)
        self.assertEqual('', server._catalog_public_reason(''))

if __name__ == '__main__':
    unittest.main()
