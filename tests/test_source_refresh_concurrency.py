"""Controlled offline source-refresh bounds; no real endpoints or credentials."""
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from cpa_probe import cpa_source_probe as csp


class SourceRefreshTests(unittest.TestCase):
    def setUp(self):
        csp._remote_cache.clear()

    def getter(self, delay=0.0):
        lock = threading.Lock()
        state = {"calls": 0, "active": 0, "peak": 0}
        def get(url, **kwargs):
            with lock:
                state["calls"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(delay)
                if "/commits/" in url:
                    return 200, json.dumps({"sha": "a" * 40})
                return 200, "package fixture"
            finally:
                with lock:
                    state["active"] -= 1
        return get, state

    def test_full_manifest_parallel_and_controlled_speed(self):
        get, state = self.getter(0.025)
        count = sum(map(len, csp.SOURCE_MANIFEST.values())) + 3
        # Same controlled delay and request count for sequential baseline.
        start = time.monotonic()
        for _ in range(count):
            get("https://source.example/fixture", timeout=10)
        serial = time.monotonic() - start
        get, state = self.getter(0.025)
        start = time.monotonic()
        with patch.object(csp, "_http_get", side_effect=get):
            result = csp.extract_remote(use_cache=False, timeout=5)
        parallel = time.monotonic() - start
        self.assertEqual(state["calls"], count)
        self.assertGreater(state["peak"], 1)
        self.assertLessEqual(state["peak"], 6)
        self.assertLess(parallel, serial * 0.7)
        self.assertEqual(set(result.revisions), set(csp.SOURCE_MANIFEST))
        self.assertTrue(result.immutable)
        self.assertFalse(result.ok)  # placeholders are not verified identities
        print(f"controlled source timing: serial={serial:.3f}s parallel={parallel:.3f}s requests={count}")

    def test_same_key_singleflight_and_negative_cache(self):
        get, state = self.getter(0.015)
        barrier = threading.Barrier(8)
        def call():
            barrier.wait()
            return csp.extract_remote(timeout=5)
        with patch.object(csp, "_http_get", side_effect=get), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: call(), range(8)))
            self.assertIs(csp.extract_remote(), results[0])
        self.assertTrue(all(x is results[0] for x in results))
        self.assertEqual(state["calls"], sum(map(len, csp.SOURCE_MANIFEST.values())) + 3)

    def test_key_isolates_refs_proxy_version_manifest_and_source(self):
        get, state = self.getter()
        with patch.object(csp, "_http_get", side_effect=get):
            first = csp.extract_remote()
            for kwargs in ({"ref": "other"}, {"cpamp_ref": "other"},
                           {"sub2api_ref": "other"}, {"proxy": "http://127.0.0.1:9999"}):
                self.assertIsNot(csp.extract_remote(**kwargs), first)
            with patch.object(csp, "_SOURCE_EXTRACTOR_VERSION", 999):
                self.assertIsNot(csp.extract_remote(), first)
            with patch.dict(csp.SOURCE_MANIFEST, {"sub2api": ("fixture.go",)}):
                self.assertIsNot(csp.extract_remote(), first)
            with patch.object(csp, "_RAW_BASE", "https://raw.githubusercontent.com/fixture/other"):
                self.assertIsNot(csp.extract_remote(), first)

    def test_expired_or_revision_modified_cache_refreshes(self):
        get, _ = self.getter()
        with patch.object(csp, "_http_get", side_effect=get):
            first = csp.extract_remote()
            first.expires_at = time.time() - 1
            second = csp.extract_remote()
            self.assertIsNot(first, second)
            second.revisions["cpa"] = "b" * 40
            self.assertIsNot(csp.extract_remote(), second)

    def test_uncooperative_read_has_caller_deadline(self):
        release = threading.Event()
        condition = threading.Condition()
        active = 0
        def get(*args, **kwargs):
            nonlocal active
            with condition:
                active += 1
            try:
                release.wait(2)
                return 200, "{}"
            finally:
                with condition:
                    active -= 1
                    condition.notify_all()
        start = time.monotonic()
        with patch.object(csp, "_http_get", side_effect=get):
            try:
                result = csp.extract_remote(timeout=0.08, use_cache=False)
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertFalse(result.ok)
                self.assertFalse(result.immutable)
            finally:
                release.set()
                with condition:
                    self.assertTrue(condition.wait_for(lambda: active == 0, timeout=2))

    def test_follower_uses_own_deadline(self):
        started = threading.Event()
        release = threading.Event()
        base_get, state = self.getter()
        def get(*args, **kwargs):
            started.set()
            release.wait(2)
            return base_get(*args, **kwargs)
        with patch.object(csp, "_http_get", side_effect=get), ThreadPoolExecutor(max_workers=1) as pool:
            owner = pool.submit(csp.extract_remote, timeout=5)
            try:
                self.assertTrue(started.wait(1))
                start = time.monotonic()
                follower = csp.extract_remote(timeout=0.04)
                self.assertLess(time.monotonic() - start, 0.5)
                self.assertFalse(follower.ok)
                self.assertIn("source refresh wait deadline exceeded", follower.errors)
            finally:
                release.set()
            result = owner.result(timeout=3)
            self.assertIs(csp.extract_remote(), result)
        self.assertEqual(state["calls"], sum(map(len, csp.SOURCE_MANIFEST.values())) + 3)

    def test_cross_key_global_capacity(self):
        get, state = self.getter(0.01)
        with patch.object(csp, "_http_get", side_effect=get), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda i: csp.extract_remote(ref=str(i), timeout=5), range(4)))
        self.assertEqual(len(results), 4)
        self.assertEqual(state["calls"], 4 * (sum(map(len, csp.SOURCE_MANIFEST.values())) + 3))
        self.assertGreater(state["peak"], 1)
        self.assertLessEqual(state["peak"], csp._SOURCE_WORKERS)

    def test_zero_budget_does_not_send(self):
        with patch.object(csp, "_http_get", side_effect=AssertionError("must not send")) as get:
            result = csp.extract_remote(timeout=0, use_cache=False)
        self.assertEqual(get.call_count, 0)
        self.assertFalse(result.ok)

    def test_unpinned_keeps_diagnostic_fields_without_success(self):
        with patch.object(csp, "_http_get", return_value=(200, "package fixture")):
            result = csp.extract_remote(use_cache=False)
        self.assertFalse(result.immutable)
        self.assertFalse(result.ok)
        self.assertEqual(set(result.revisions), set(csp.SOURCE_MANIFEST))
        self.assertTrue(result.coverage)
        self.assertTrue(result.snapshot_id)


if __name__ == "__main__":
    unittest.main()
