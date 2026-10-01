"""Remote catalogue cache contracts: offline, deterministic and isolated."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cpa_probe import model_catalog as mc


class CatalogueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'IMPORTER_CATALOG_CACHE_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cache = patch.object(mc, '_cache', {'at': 0., 'names': None, 'ok': False, 'why': ''})
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_no_cache_does_not_read_last_good(self):
        mc._disk_cache_save(['gpt-99-fixture'])
        with patch.object(mc, '_http_json', side_effect=OSError('offline')):
            names, reason = mc.remote_names(use_cache=False)
        self.assertEqual([], names)
        self.assertNotIn('落盘', reason)

    def test_no_cache_does_not_overwrite_last_good(self):
        mc._disk_cache_save(['gpt-99-fixture'])
        before = Path(mc._disk_cache_path()).read_bytes()
        with patch.object(mc, '_http_json', return_value={'provider': [{'id': 'gpt-100-fixture'}]}):
            self.assertEqual(['gpt-100-fixture'], mc.remote_names(use_cache=False)[0])
        self.assertEqual(before, Path(mc._disk_cache_path()).read_bytes())

    def test_cached_failure_returns_last_good_with_provenance(self):
        mc._disk_cache_save(['gpt-99-fixture'])
        with patch.object(mc, '_http_json', side_effect=OSError('offline')):
            names, reason = mc.remote_names()
        self.assertEqual(['gpt-99-fixture'], names)
        self.assertIn('落盘', reason)

    def test_malformed_disk_is_a_cache_miss(self):
        p = Path(self.temp.name) / mc._DISK_CACHE_NAME
        for value in ([], 42, 'bad', {'names': [], 'at': 0}, {'names': ['x'], 'at': 'bad'}):
            p.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(([], 0.0), mc._disk_cache_load())

    def test_concurrent_disk_writers_use_unique_temporary_files(self):
        original = os.replace
        original_dump = json.dump
        barrier = threading.Barrier(2)
        temps = []
        failures = []
        def dump(value, stream, **kwargs):
            temps.append(stream.name)
            barrier.wait(timeout=3)
            return original_dump(value, stream, **kwargs)
        def replace(src, dst):
            try:
                original(src, dst)
            except OSError as exc:
                failures.append(type(exc).__name__)
                raise
        with patch.object(mc.os, 'replace', side_effect=replace), \
                patch.object(mc.json, 'dump', side_effect=dump):
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(mc._disk_cache_save, [['gpt-99-a'], ['gpt-99-b']]))
        self.assertEqual(2, len(set(temps)))
        self.assertEqual([], failures)
        self.assertIn(mc._disk_cache_load()[0], [['gpt-99-a'], ['gpt-99-b']])

    def test_first_success_returns_without_waiting_for_slow_mirror(self):
        slow_started = threading.Event()
        release = threading.Event()
        done = threading.Event()
        result = []
        def http(url, **kwargs):
            if url == mc._CATALOG_URLS[0]:
                slow_started.set()
                release.wait(timeout=3)
                raise OSError('slow mirror')
            self.assertTrue(slow_started.wait(timeout=2))
            return {'provider': [{'id': 'gpt-99-fixture'}]}
        def work():
            try:
                result.append(mc.remote_names(use_cache=False))
            finally:
                done.set()
        with patch.object(mc, '_http_json', side_effect=http):
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(done.wait(timeout=1), 'first result still waits for the slow mirror')
            finally:
                release.set()
                thread.join(timeout=4)
        self.assertEqual(['gpt-99-fixture'], result[0][0])




class UnreachableHostBudgetTests(unittest.TestCase):
    """A host that never answers must not cost a full probe per credential.

    `000` means "no answer at all" and is deliberately excluded from the
    status-code streak, so an unreachable host keeps every later credential
    paying the full probe. On a VPS with poor egress that is the dominant
    cost of a full re-detection and shows up as a stalled poll in the UI.
    """

    def test_repeated_connection_failures_stop_reprobing_the_same_section(self):
        from cpa_probe.pipeline import Prober, SectionVerdict, Attempt
        import cpa_probe as cpa

        rows = cpa.parse_lines('\n'.join(
            f'https://unreachable.example,sk-fixture-{i:04d}' for i in range(6)),
            allow_private=True).valid
        prober = Prober(gap=0., timeout=1, probe_context=False, swap_samples=0,
                        probe_capabilities=False, workers=1)
        probes = []

        def never_answers(row, section):
            probes.append((row.api_key, section))
            return SectionVerdict(
                section=section, usable=False, base_url=row.base_for(section),
                models=[], catalog=[], category='未知', action='连接失败',
                attempts=[Attempt(section=section, model='', combo='baseline',
                                  status='000', category='未知',
                                  action='连接失败', elapsed_ms=1)])

        with patch.object(Prober, '_full_probe', side_effect=never_answers,
                          autospec=False):
            results = [prober.probe(r) for r in rows]

        section = cpa.SECTIONS[0]
        per_section = [p for p in probes if p[1] == section]
        self.assertTrue(all(not v.usable for r in results
                            for v in r.sections.values()))
        self.assertLessEqual(len(per_section), 3,
            f'unreachable host probed {len(per_section)} times for one section; '
            'the connection-failure streak never opens the breaker')


class ConnectionFailureBreakerTests(unittest.TestCase):
    """A host that never answers must stop costing a full probe per credential.

    `000` means "no answer at all". It deliberately neither advances nor resets
    the status-code streak, so a host that is simply unreachable keeps every
    later credential paying the full request timeout. With poor egress that is
    the dominant cost of a full re-detection, and the UI only sees a poll that
    never advances.
    """

    def _run(self, statuses, keys=8):
        from cpa_probe.pipeline import Prober, SectionVerdict, Attempt
        import cpa_probe as cpa

        # Each section gets its own sequence: they are probed independently and
        # a shared iterator would split one host's streak across four sections.
        seqs = {}
        calls = []

        def probe(row, section):
            seq = seqs.setdefault(section, iter(statuses))
            status = next(seq, statuses[-1])
            calls.append((row.api_key, section, status))
            category = '未知' if status == '000' else '临时'
            return SectionVerdict(
                section=section, usable=False, base_url=row.base_for(section),
                models=[], catalog=[], category=category, action=status,
                attempts=[Attempt(section=section, model='', combo='baseline',
                                  status=status, category=category,
                                  action=status, elapsed_ms=1)])

        lines = [f'https://flaky.example,sk-k-{i:04d}' for i in range(keys)]
        rows = cpa.parse_lines(chr(10).join(lines), allow_private=True).valid
        prober = Prober(gap=0., timeout=1, probe_context=False, swap_samples=0,
                        probe_capabilities=False, workers=1)
        with patch.object(Prober, '_full_probe', side_effect=probe,
                          autospec=False):
            [prober.probe(r) for r in rows]
        section = cpa.SECTIONS[0]
        return [c for c in calls if c[1] == section]

    def test_an_unreachable_host_stops_being_reprobed(self):
        probes = self._run(['000'] * 64)
        self.assertLessEqual(len(probes), 4,
            f'unreachable host probed {len(probes)} times for one section')

    def test_a_dropped_connection_does_not_reset_a_status_code_streak(self):
        # `000` between two 502s is not evidence that the host's behaviour
        # changed, so it must neither advance nor reset the streak. Letting it
        # reset was measured in 2026-09-16 to restart the breaker on a stably
        # failing host after any single network blip.
        probes = self._run(['502', '000'] * 32)
        self.assertLessEqual(len(probes), 6,
            f'a dropped connection reset the 502 streak; {len(probes)} probes '
            'ran where the breaker should have opened after three 502s')
        statuses = [p[2] for p in probes]
        self.assertEqual(3, statuses.count('502'),
            f'the breaker must open on the third 502, got {statuses}')


if __name__ == '__main__':
    unittest.main()
