"""Offline admission tests: no network and no reduction in probe coverage."""
import sys
import importlib.util
import threading
import time
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("host_limiter_under_test",
    Path(__file__).resolve().parents[1] / "cpa_probe" / "resources.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
HostLimiter = module.HostLimiter


class HostLimiterFairness(unittest.TestCase):
    def wait_queue(self, limiter, host, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with limiter.condition:
                if len(limiter.waiters.get(host, [])) == count:
                    return
            time.sleep(.005)
        self.fail("waiter did not reach admission queue")

    def test_fifo_across_jobs_on_same_host(self):
        limiter, order, threads = HostLimiter(1), [], []
        with limiter.slot("a", 0, lambda: None):
            for i in range(6):
                def worker(index=i):
                    with limiter.slot("a", 0, lambda: None):
                        order.append(index)
                thread = threading.Thread(target=worker, daemon=True)
                threads.append(thread)
                thread.start()
                self.wait_queue(limiter, "a", i + 1)
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, list(range(6)))
        self.assertEqual(limiter.waiters, {})
        self.assertEqual(limiter.active["a"], 0)

    def test_cancelled_head_does_not_block_next_or_other_host(self):
        limiter = HostLimiter(1)
        cancel, cancelled, next_entered, other_entered = [threading.Event() for _ in range(4)]
        def check():
            if cancel.is_set():
                raise RuntimeError("cancelled fixture")
        def head():
            try:
                with limiter.slot("a", 0, check):
                    pass
            except RuntimeError:
                cancelled.set()
        def next_worker(host, event):
            with limiter.slot(host, 0, lambda: None):
                event.set()
        with limiter.slot("a", 0, lambda: None):
            first = threading.Thread(target=head, daemon=True)
            first.start()
            self.wait_queue(limiter, "a", 1)
            second = threading.Thread(target=next_worker, args=("a", next_entered), daemon=True)
            second.start()
            self.wait_queue(limiter, "a", 2)
            other = threading.Thread(target=next_worker, args=("b", other_entered), daemon=True)
            other.start()
            self.assertTrue(other_entered.wait(3))
            cancel.set()
            self.assertTrue(cancelled.wait(3))
            self.wait_queue(limiter, "a", 1)
            self.assertFalse(next_entered.is_set())
        self.assertTrue(next_entered.wait(3))
        for thread in (first, second, other):
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(limiter.waiters, {})

    def test_concurrency_and_start_gap_are_preserved(self):
        limiter, release, starts = HostLimiter(2), threading.Event(), []
        def worker():
            with limiter.slot("a", .04, lambda: None):
                starts.append(time.monotonic())
                release.wait(3)
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(3)]
        try:
            for thread in threads:
                thread.start()
            self.wait_queue(limiter, "a", 1)
            deadline = time.monotonic() + 3
            while len(starts) < 2 and time.monotonic() < deadline:
                time.sleep(.005)
            with limiter.condition:
                self.assertEqual(limiter.active["a"], 2)
            self.assertEqual(len(starts), 2)
        finally:
            release.set()
            for thread in threads:
                thread.join(3)
        self.assertEqual(len(starts), 3)
        self.assertTrue(all(b-a >= .035 for a, b in zip(starts, starts[1:])))
        self.assertEqual(limiter.waiters, {})


if __name__ == "__main__":
    unittest.main()
