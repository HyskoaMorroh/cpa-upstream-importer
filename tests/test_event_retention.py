"""Bounded event history must describe actual retention and lost events."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Job


class EventRetentionTests(unittest.TestCase):
    def test_repeated_truncation_counts_only_lost_events_and_keeps_boundaries(self):
        job = Job('fixture-retention', [], {})
        job.MAX_EVENTS = 5
        job.KEEP_HEAD = 2
        for index in range(12):
            job.emit('info', {'msg': f'fixture-event-{index}'})
        retained = [event for event in job.events if not event.get('_trunc')]
        notices = [event for event in job.events if event.get('_trunc')]
        self.assertEqual(len(job.events), job.MAX_EVENTS)
        self.assertEqual([event['msg'] for event in retained],
                         ['fixture-event-0', 'fixture-event-1',
                          'fixture-event-10', 'fixture-event-11'])
        self.assertEqual(job.dropped, 12 - len(retained))
        self.assertEqual(len(notices), 1)
        self.assertIn(f'省略 {job.dropped} 条', notices[0]['msg'])
        self.assertIn('保留任务开头与最近事件', notices[0]['msg'])
        self.assertIn('省略内容无法从本次事件导出恢复', notices[0]['msg'])
        self.assertNotIn('完整记录', notices[0]['msg'])
        self.assertNotIn('stderr', notices[0]['msg'])

    def test_history_at_limit_does_not_claim_events_were_lost(self):
        job = Job('fixture-no-truncation', [], {})
        job.MAX_EVENTS = 5
        job.KEEP_HEAD = 2
        for index in range(job.MAX_EVENTS):
            job.emit('info', {'msg': f'fixture-event-{index}'})
        self.assertEqual(job.dropped, 0)
        self.assertFalse(any(event.get('_trunc') for event in job.events))
        self.assertEqual(len(job.events), job.MAX_EVENTS)


if __name__ == '__main__':
    unittest.main()
