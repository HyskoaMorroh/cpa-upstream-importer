"""Regression coverage for bounded asynchronous planning admission."""
from __future__ import annotations

import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class PlanCapacityTests(unittest.TestCase):
    def setUp(self):
        self.store = server.Store()
        self.limit = getattr(self.store, "MAX_PLAN_TASKS", 32)

    def add(self, number, state="running", finished=0.0):
        task = server.PlanTask(f"plan-{number}", {})
        task.state = state
        task.finished = finished
        self.store.add_plan_task(task)
        return task

    def fill(self, state="running", finished=0.0):
        return [self.add(i, state, finished) for i in range(self.limit)]

    def test_running_tasks_reject_over_capacity_without_losing_results(self):
        retained = self.fill()
        with self.assertRaises(server.CapacityError):
            self.add("overflow")
        self.assertEqual(len(self.store.plan_tasks), self.limit)
        for task in retained:
            self.assertIs(self.store.get_plan_task(task.id), task)

    def test_recent_finished_tasks_keep_their_polling_window(self):
        with mock.patch.object(server.time, "time", return_value=1000.0):
            retained = self.fill("done", 1000.0)
            retained[0].result = {"plan_id": "already-computed"}
            with self.assertRaises(server.CapacityError):
                self.add("overflow")
            self.assertEqual(len(self.store.plan_tasks), self.limit)
            self.assertEqual(self.store.get_plan_task(retained[0].id).result,
                             {"plan_id": "already-computed"})

    def test_old_terminal_task_can_make_room_without_exceeding_capacity(self):
        with mock.patch.object(server.time, "time", return_value=1000.0):
            self.fill("done", 699.0)
            admitted = self.add("replacement")
        self.assertEqual(len(self.store.plan_tasks), self.limit)
        self.assertIs(self.store.get_plan_task(admitted.id), admitted)

    def test_expired_terminal_tasks_are_reclaimed(self):
        with mock.patch.object(server.time, "time", return_value=1000.0):
            self.fill("error", 1000.0)
        with mock.patch.object(server.time, "time",
                               return_value=1001.0 + self.store.TTL):
            task = self.add("after-ttl")
        self.assertEqual(set(self.store.plan_tasks), {task.id})

    def test_ttl_never_evicts_running_tasks_to_admit_new_work(self):
        with mock.patch.object(server.time, "time", return_value=1000.0):
            retained = self.fill()
        with mock.patch.object(server.time, "time",
                               return_value=1001.0 + self.store.TTL):
            with self.assertRaises(server.CapacityError):
                self.add("after-ttl")
        self.assertEqual(set(self.store.plan_tasks), {t.id for t in retained})

    def test_concurrent_admission_obeys_one_shared_limit(self):
        def admit(number):
            try:
                self.add(number)
            except server.CapacityError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=12) as pool:
            admitted = list(pool.map(admit, range(self.limit + 16)))
        self.assertEqual(sum(admitted), self.limit)
        self.assertEqual(len(self.store.plan_tasks), self.limit)

    def test_rejected_plan_does_not_start_a_background_thread(self):
        self.fill()
        job = SimpleNamespace(id="fixture-job", state="done", opts={}, results=[])
        self.store.add_job(job)
        handler, _, _ = response_handler()
        handler._load_cfg = lambda: ("# synthetic configuration\n", {})
        with mock.patch.object(server, "STORE", self.store), \
                mock.patch.dict(server.Handler._plan_cache, {}, clear=True), \
                mock.patch.object(server.threading, "Thread") as thread:
            with self.assertRaises(server.CapacityError):
                handler._api_plan({"job_id": job.id})
            thread.assert_not_called()
        self.assertEqual(len(self.store.plan_tasks), self.limit)


class RetryAfterTests(unittest.TestCase):
    def test_retryable_capacity_response_includes_positive_retry_after(self):
        for status in (429, 503):
            with self.subTest(status=status):
                handler, codes, headers = response_handler()
                handler._json(status, {"error": "busy", "retryable": True})
                self.assertEqual(codes, [status])
                self.assertIn("Retry-After", headers)
                self.assertGreater(int(headers["Retry-After"]), 0)
                body = json.loads(handler.wfile.getvalue())
                self.assertTrue(body["retryable"])

    def test_success_does_not_advertise_retry_after(self):
        handler, codes, headers = response_handler()
        handler._json(200, {"ok": True})
        self.assertEqual(codes, [200])
        self.assertNotIn("Retry-After", headers)


def response_handler():
    handler = object.__new__(server.Handler)
    codes, headers = [], {}
    handler.wfile = io.BytesIO()
    handler.send_response = codes.append
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    return handler, codes, headers


if __name__ == "__main__":
    unittest.main()
