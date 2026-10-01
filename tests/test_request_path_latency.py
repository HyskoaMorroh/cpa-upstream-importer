"""No request path may wait on public GitHub.

The bounded source manifest spans three repositories and costs a 60s budget on
a cold cache. Whenever that fetch sits in a request path, a VPS with poor
egress stalls before any real work starts, which the UI reports as an
unresponsive poll rather than as a network problem.
"""
from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import cpa_source_probe as csp
from cpa_probe.pipeline import Prober

BUDGET = 2.0


class ColdCacheLatencyTests(unittest.TestCase):
    def setUp(self):
        csp._remote_cache.clear() if hasattr(csp._remote_cache, 'clear') else None
        csp._ident_cache.clear()
        self.calls = []
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def slow_get(url, *, timeout=0, proxy=None, **kw):
            self.calls.append(url)
            self.release.wait(timeout=min(timeout or 30, 30))
            raise OSError('synthetic unreachable upstream')

        self.http = patch.object(csp, '_http_get', side_effect=slow_get)
        self.http.start()
        self.addCleanup(self.http.stop)

    def test_prober_construction_does_not_wait_on_github(self):
        start = time.monotonic()
        prober = Prober(gap=0., timeout=1, probe_context=False, swap_samples=0,
                        probe_capabilities=False, workers=1)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, BUDGET,
            f'Prober() blocked {elapsed:.1f}s on a cold source-manifest cache')
        self.assertIsNotNone(prober.source_identity,
            'a non-blocking construction must still expose an identity object')

    def test_identity_snapshot_upgrades_once_the_refresh_lands(self):
        snapshot = csp.identity_nonblocking()
        self.assertFalse(snapshot.ok,
            'an unreachable manifest must report itself as incomplete')
        self.assertTrue(any(snapshot.uncertainty),
            'an incomplete snapshot must say why, never look authoritative')




class FirstPaintLatencyTests(unittest.TestCase):
    """`/api/context` must never wait on the public model catalogue.

    A cold catalogue costs `timeout + 4` seconds before the first paint, and on
    a VPS without egress that repeats every ten minutes. The screen then shows
    empty fields with no stated reason, which reads as data loss rather than as
    a network problem.
    """

    def setUp(self):
        from cpa_probe import model_catalog as mc
        self.mc = mc
        self.cache = patch.object(mc, '_cache',
                                  {'at': 0., 'names': None, 'ok': False, 'why': ''})
        self.cache.start()
        self.addCleanup(self.cache.stop)
        mc.reset_remote_nonblocking()
        self.addCleanup(mc.reset_remote_nonblocking)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def slow(url, *, timeout=0, proxy=None):
            self.release.wait(timeout=min(timeout or 30, 30))
            raise OSError('synthetic unreachable catalogue')

        http = patch.object(mc, '_http_json', side_effect=slow)
        http.start()
        self.addCleanup(http.stop)
        disk = patch.object(mc, '_disk_cache_load', return_value=([], 0.0))
        disk.start()
        self.addCleanup(disk.stop)

    def test_catalogue_lookup_returns_immediately(self):
        start = time.monotonic()
        names, why = self.mc.remote_names_nonblocking()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, BUDGET,
            f'the catalogue lookup blocked {elapsed:.1f}s on the request path')
        self.assertEqual([], names)
        self.assertTrue(why, 'an empty catalogue must state why it is empty')

    def test_first_paint_states_why_the_market_signal_is_missing(self):
        import server
        top, by_line, why = server._market_top_gen({})
        self.assertEqual(({}, {}), (top, by_line))
        self.assertTrue(why,
            'a missing market signal must be explained, never silently empty')


if __name__ == '__main__':
    unittest.main()
