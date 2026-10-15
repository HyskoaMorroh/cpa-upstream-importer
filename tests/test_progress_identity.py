"""Progress identities count credentials without exposing them or changing probes."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from cpa_probe.batch import BatchProber
from cpa_probe.parse import ParsedRow
from cpa_probe.pipeline import CandidateResult


class ProgressIdentityTests(unittest.TestCase):
    def test_same_site_keeps_original_remaining_age(self):
        job = server.Job('fixture', [], {})
        for elapsed, identity in ((10, 'candidate-0'), (20, 'candidate-1')):
            with patch.object(server.time, 'time', return_value=job.started + elapsed):
                job.mark_unit_start('fixture.invalid', unit_id=identity)
        with patch.object(server.time, 'time', return_value=job.started + 30):
            self.assertEqual(2, job._progress(2)['in_flight'])
            job.mark_unit_done('fixture.invalid', unit_id='candidate-1')
            progress = job._progress(2)
        self.assertEqual(1, progress['in_flight'])
        self.assertEqual(20, progress['slowest_age'])
        self.assertEqual('fixture.invalid', progress['slowest_host'])
        self.assertEqual(1, progress['unit_done'])

    def test_batch_pairs_same_site_units_including_failure(self):
        for failing in (False, True):
            with self.subTest(failing=failing):
                job = server.Job('fixture', [], {})
                rows = [ParsedRow(i, '', 'https://fixture.invalid', f'SECRET-FIXTURE-{i}')
                        for i in range(2)]
                both_started = threading.Barrier(2)
                first_completed = threading.Event()
                records, remaining = [], []
                class FixtureProber:
                    def probe(self, row):
                        both_started.wait(timeout=3)
                        if row.line_no == 0:
                            if not first_completed.wait(3):
                                raise AssertionError('completion callback not delivered')
                        elif failing:
                            raise ValueError('fixture failure')
                        return CandidateResult(row=row)
                def progress(current, total, site, stats):
                    records.append((current, site, dict(stats)))
                    if current == 0:
                        job.mark_unit_start(site, unit_id=stats.get('_unit_id'))
                    else:
                        job.mark_unit_done(site, unit_id=stats.get('_unit_id'))
                        if current == 1:
                            remaining.append(job._progress(2)['in_flight'])
                            first_completed.set()
                batch = BatchProber(FixtureProber(), max_workers=2)
                results = batch.probe_batch(rows, progress)
                self.assertEqual([1], remaining)
                self.assertEqual(0, job._progress(2)['in_flight'])
                self.assertEqual(2, job._progress(2)['unit_done'])
                self.assertEqual(1 if failing else 2, len(results))
                starts = {st['_unit_id'] for c, _, st in records if c == 0}
                finishes = {st['_unit_id'] for c, _, st in records if c > 0}
                self.assertEqual({'candidate-0', 'candidate-1'}, starts)
                self.assertEqual(starts, finishes)
                self.assertNotIn('SECRET', repr(records))
                self.assertNotIn('_unit_id', batch._stats)

    def test_legacy_name_only_api_is_preserved(self):
        job = server.Job('fixture', [], {})
        job.mark_unit_start('fixture.invalid')
        self.assertEqual('fixture.invalid', job._progress(1)['slowest_host'])
        job.mark_unit_done('fixture.invalid')
        self.assertEqual(0, job._progress(1)['in_flight'])


if __name__ == '__main__':
    unittest.main()
