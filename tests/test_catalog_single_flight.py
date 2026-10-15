"""同步名录拉取的单飞（single-flight）—— 2026-10-12 加。

现场（侦察核实，代码逐行读过）
----------------------------
`remote_names()` 的同步路径原来只查一次内存缓存、没有任何锁。冷缓存时同一批
次的 4 个段线程同时未命中，于是各起一个线程池打两个镜像地址（最长 12 s）、
各自 `_disk_cache_save` → `os.replace` 互相竞争；GitHub 不通时 `_TTL_BAD=600`，
这场风暴每 10 分钟对整批重来一次。`remote_names_nonblocking` 早有
`_NB_LOCK` + `inflight` 的单飞模式，同步路径没有。

本文件钉三条不变式：
  1. N 个线程同时冷命中 → 只拉一次，其余拿到同一份结果；
  2. `use_cache=False` 不进单飞 —— 强制刷新与隔离测试要的就是独立拉取；
  3. 拉取失败不会把锁卡死（异常后其余线程仍能继续）。

不发任何网络请求：`_remote_names_fetch` 被替换成受控的假实现。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import model_catalog as mc


def _reset_cache() -> None:
    mc._cache.clear()
    mc._cache.update(at=0.0, names=None, ok=False, why="")


class SyncCatalogSingleFlightTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def _slow_fetch(self, calls: list, *, delay: float = 0.25,
                    fail: bool = False):
        """假拉取：记录调用、模拟延迟，并像真实现一样回填 `_cache`。

        必须回填 `_cache`：单飞的去重靠的是「拿到锁后再查一次缓存」命中。
        假实现不回填，锁只会把拉取串行化而不会去重 —— 那正是要钉住的差别。
        """
        def fake(*, timeout, proxy, use_cache, key, now):
            calls.append(threading.current_thread().name)
            time.sleep(delay)
            if fail:
                raise RuntimeError("synthetic fetch failure")
            names = ["fixture-model-a", "fixture-model-b"]
            if use_cache:
                mc._cache.update(key=key, at=time.time(), names=names,
                                 ok=True, why="")
            return list(names), ""
        return fake

    def test_concurrent_cold_callers_fetch_once(self):
        calls: list = []
        results: list = []
        errors: list = []

        def worker():
            try:
                results.append(mc.remote_names())
            except Exception as exc:  # noqa: BLE001 —— 收集而不是丢失
                errors.append(exc)

        with patch.object(mc, "_remote_names_fetch", self._slow_fetch(calls)):
            threads = [threading.Thread(target=worker, name=f"sec-{i}")
                       for i in range(4)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len(calls), 1,
                         f"4 个线程冷命中必须只拉一次，实得 {len(calls)} 次：{calls}")
        self.assertEqual({tuple(r[0]) for r in results},
                         {("fixture-model-a", "fixture-model-b")},
                         "其余线程必须拿到首个拉取填好的同一份缓存")

    def test_use_cache_false_bypasses_single_flight(self):
        calls: list = []
        with patch.object(mc, "_remote_names_fetch", self._slow_fetch(calls, delay=0.0)):
            for _ in range(3):
                mc.remote_names(use_cache=False)
        self.assertEqual(len(calls), 3,
                         "use_cache=False 每次都必须独立拉取（强制刷新 / 隔离测试）")
        self.assertIsNone(mc._cache["names"],
                          "use_cache=False 不得污染共享内存缓存")

    def test_fetch_failure_does_not_wedge_the_lock(self):
        calls: list = []
        with patch.object(mc, "_remote_names_fetch",
                          self._slow_fetch(calls, delay=0.0, fail=True)):
            with self.assertRaises(RuntimeError):
                mc.remote_names()
        # 锁必须已释放：下一次调用能进到拉取（而不是永远等锁）。
        done = threading.Event()

        def second():
            with patch.object(mc, "_remote_names_fetch",
                              self._slow_fetch(calls, delay=0.0)):
                mc.remote_names()
            done.set()

        threading.Thread(target=second, daemon=True).start()
        self.assertTrue(done.wait(timeout=5),
                        "拉取抛异常后锁没有释放 —— 后续调用被卡死")
        self.assertEqual(len(calls), 2)

    def test_warm_cache_never_takes_the_fetch_path(self):
        calls: list = []
        key = (tuple(mc._CATALOG_URLS), None, 8)
        mc._cache.update(key=key, at=time.time(), names=["warm-model"],
                         ok=True, why="")
        with patch.object(mc, "_remote_names_fetch", self._slow_fetch(calls, delay=0.0)):
            names, why = mc.remote_names()
        self.assertEqual(calls, [], "缓存命中时不得拉取")
        self.assertEqual(names, ["warm-model"])
        self.assertEqual(why, "")


if __name__ == "__main__":
    unittest.main()
