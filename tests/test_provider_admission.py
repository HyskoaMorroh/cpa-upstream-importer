"""Local fairness must not mutate shared policy reservations."""
import concurrent.futures
import threading
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe.resources import AdmissionTimeout, ProviderAdmission


class ProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.gate = ProviderAdmission()
        self.pool = concurrent.futures.ThreadPoolExecutor(8)
        self.addCleanup(self.pool.shutdown, wait=True)

    def queued(self, key, size):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            with self.gate.condition:
                if len(self.gate.queues.get(key, [])) == size:
                    return
            time.sleep(.001)
        self.fail('worker did not enter admission queue')

    def test_fifo_and_independent_provider(self):
        order = []
        def enter(key, number):
            with self.gate.slot(key, 1, lambda: None):
                order.append(number)
        with self.gate.slot('same', 1, lambda: None):
            a = self.pool.submit(enter, 'same', 1)
            self.queued('same', 2)
            b = self.pool.submit(enter, 'same', 2)
            self.queued('same', 3)
            self.pool.submit(enter, 'different', 0).result(1)
            self.assertEqual([0], order)
        a.result(1)
        b.result(1)
        self.assertEqual([0, 1, 2], order)
        self.assertEqual({}, self.gate.queues)

    def test_cancelled_middle_waiter_does_not_release_owner(self):
        cancelled = threading.Event()
        def check():
            if cancelled.is_set():
                raise concurrent.futures.CancelledError()
        def enter(check):
            with self.gate.slot('same', 1, check):
                return True
        with self.gate.slot('same', 1, lambda: None):
            middle = self.pool.submit(enter, check)
            self.queued('same', 2)
            last = self.pool.submit(enter, lambda: None)
            self.queued('same', 3)
            cancelled.set()
            with self.assertRaises(concurrent.futures.CancelledError):
                middle.result(1)
            self.queued('same', 2)
            self.assertFalse(last.done())
        self.assertTrue(last.result(1))
        self.assertEqual({}, self.gate.queues)

    def test_exception_releases_owner(self):
        with self.assertRaises(ValueError):
            with self.gate.slot('same', 1, lambda: None):
                raise ValueError('fixture')
        self.assertEqual({}, self.gate.queues)
        self.assertEqual({}, self.gate.owners)

    def test_same_provider_reentry_fails_without_releasing_owner(self):
        with self.gate.slot('same', 1, lambda: None):
            with self.assertRaisesRegex(RuntimeError, 'not reentrant'):
                with self.gate.slot('same', 1, lambda: None):
                    self.fail('nested owner admitted')
            self.assertEqual(1, len(self.gate.queues['same']))
            self.assertEqual(threading.get_ident(), self.gate.owners['same'])
            with self.gate.slot('different', 1, lambda: None):
                self.assertEqual(2, len(self.gate.owners))
        self.assertEqual({}, self.gate.queues)
        self.assertEqual({}, self.gate.owners)
        with self.gate.slot('same', 1, lambda: None):
            self.assertEqual(threading.get_ident(), self.gate.owners['same'])
        self.assertEqual({}, self.gate.owners)

    def test_later_arrivals_cannot_extend_frozen_expiry(self):
        def enter(budget, deadline=None):
            with self.gate.slot('same', budget, lambda: None, deadline):
                return True
        with self.gate.slot('same', .03, lambda: None):
            short = self.pool.submit(enter, .03)
            self.queued('same', 2)
            later = self.pool.submit(enter, 5)
            self.queued('same', 3)
            with self.assertRaises(AdmissionTimeout):
                short.result(.5)
            self.assertFalse(later.done())
        self.assertTrue(later.result(1))

    def test_global_deadline_caps_queue_allowance(self):
        def enter():
            with self.gate.slot('same', 10, lambda: None, time.monotonic() + .03):
                self.fail('expired waiter admitted')
        with self.gate.slot('same', 10, lambda: None):
            waiter = self.pool.submit(enter)
            with self.assertRaises(AdmissionTimeout):
                waiter.result(.5)
            self.queued('same', 1)


if __name__ == '__main__':
    unittest.main()
