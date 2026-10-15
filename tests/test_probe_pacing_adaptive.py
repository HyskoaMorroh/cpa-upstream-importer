"""探测节拍与站方属性复用的提速不变式 —— 2026-10-13 本机实跑后加。

现场（本机 Docker Desktop，真实 config.yaml，10 把同域名真实 Key）
----------------------------------------------------------------
第一轮实跑：429 次推理尝试，墙钟 1913.8 秒，单次成本 4.46 秒。而同一台
容器里对该站实测的并发容忍度是：

    1 路   墙钟 1.47s   全 200
    4 路   墙钟 1.36s   全 200
   10 路   墙钟 1.42s   全 200
   20 路   墙钟 1.40s   全 200     ← 零间隔，20 个全部成功
   串行 12 次零间隔   墙钟 15.8s   全 200

也就是说该站根本没有 bulk probe guard，等待占墙钟 ~98%，全是自己造的：
`Prober.gap` 的默认 3.0 秒来自**另一个**站的 guard 实测值，却被当成所有
站的起始节拍，而前端又把 `value="3"` 硬写进输入框、`candidate_workers`
硬传 4 压掉后端按站数自适应的 16。

本文件钉四条不变式（全部离线，不发任何网络请求）：
  1. `gap=None`（前端留空）走乐观起点 `_OPTIMISTIC_GAP`，不是 3 秒；
     显式给值仍是地板，自适应只在其上加。
  2. 撞限流时即使正文里没有确切阈值，也按 `_BACKOFF_LADDER` 退避 ——
     这是乐观起点的安全网，缺了它会一路 0.15 秒硬打。
  3. 429 与「限流」类同样触发学习，不只是带 `bulk probe` 字样的「限频」。
  4. 站方属性（静默换模、上下文窗口）按 (host, section, model) 跨凭据
     复用一次实测结果；凭据相关的结论（能用哪些模型）不受影响。

调序那一项（节拍移到服务商准入之外）在 test_probe_scheduling.py 覆盖。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import pipeline
from cpa_probe import cpa_source_probe
from cpa_probe.pipeline import Prober


def _prober(**kw):
    """造一个不会发请求、不碰账本的 Prober。

    `cached_identity` 会拉远程源码快照 —— 探测节拍的测试不需要它。
    `Prober.__init__` 里是函数内局部 import，所以 patch 要打在**源模块**
    `cpa_probe.cpa_source_probe` 上，打在 pipeline 上会 AttributeError。
    """
    with patch.object(cpa_source_probe, "cached_identity", lambda **_: _Identity()):
        return Prober(**kw)


class _Identity:
    snapshot_id = "test"

    def __deepcopy__(self, memo):
        return self


class OptimisticGapTests(unittest.TestCase):
    def test_default_gap_is_optimistic_not_three_seconds(self):
        """默认不再是 3 秒。3 秒 × 429 次 ≈ 1287 秒纯睡就是这么来的。"""
        prober = _prober()
        self.assertEqual(prober.gap, Prober._OPTIMISTIC_GAP)
        self.assertLess(prober.gap, 1.0, "乐观起点必须远小于旧的 3 秒地板")
        self.assertGreater(prober.gap, 0.0, "也不能是 0 —— 零间隔对任何站都最可疑")
        self.assertFalse(prober._gap_is_explicit)

    def test_explicit_gap_is_a_floor_and_is_marked_explicit(self):
        """操作员显式给值时那是地板，不被乐观起点覆盖。"""
        prober = _prober(gap=5.0)
        self.assertEqual(prober.gap, 5.0)
        self.assertTrue(prober._gap_is_explicit)

    def test_explicit_zero_is_honored_not_treated_as_missing(self):
        """`gap=0` 是「操作员要求零间隔」，不是「没给」。

        前端侧对应 `gapRaw === ''` 与 `'0'` 的区分；后端 `_clamp` 的
        `default=None` 分支保证缺键才回落自适应。
        """
        prober = _prober(gap=0)
        self.assertEqual(prober.gap, 0.0)
        self.assertTrue(prober._gap_is_explicit)


class BackoffLadderTests(unittest.TestCase):
    def setUp(self):
        self.prober = _prober()
        self.events: list[tuple[str, dict]] = []
        self.prober.on_event = lambda name, payload: self.events.append((name, payload))

    def test_site_threshold_in_body_wins(self):
        """正文里有确切阈值就用它（原有行为，不得回退）。"""
        learned = self.prober._note_rate_limit(
            "h1", "bulk probe guard: ip 1.2.3.4 requested 4 distinct models in 60s")
        self.assertTrue(learned)
        # 60/4 × 1.1 = 16.5
        self.assertAlmostEqual(self.prober._host_gap["h1"], 16.5, places=3)
        name, payload = self.events[-1]
        self.assertEqual(name, "rate-limit-learned")
        self.assertEqual(payload["source"], "site-threshold")

    def test_body_without_threshold_still_backs_off(self):
        """没有阈值也要退 —— 这是乐观起点的安全网。

        旧实现在这里返回 False 且什么都不做；配合 3 秒地板尚可接受，
        配合 0.15 秒起点就会一路硬打。
        """
        host = "h2"
        seen = []
        for _ in range(len(Prober._BACKOFF_LADDER)):
            self.assertTrue(self.prober._note_rate_limit(host, "429 too many requests"))
            seen.append(self.prober._host_gap[host])
        self.assertEqual(seen, list(Prober._BACKOFF_LADDER))
        # 阶梯爬满后不再变化，也不再报新事件。
        self.assertFalse(self.prober._note_rate_limit(host, "429 too many requests"))
        self.assertEqual(self.prober._host_gap[host], Prober._BACKOFF_LADDER[-1])

    def test_ladder_is_monotonic_and_starts_above_optimistic(self):
        """阶梯必须单调递增，且第一档就高于乐观起点 —— 否则退避等于没退。"""
        ladder = Prober._BACKOFF_LADDER
        self.assertEqual(list(ladder), sorted(ladder))
        self.assertGreater(ladder[0], Prober._OPTIMISTIC_GAP)
        self.assertLessEqual(ladder[-1], Prober._MAX_LEARNED_GAP)

    def test_learned_gap_never_decreases(self):
        """先学到大值，再撞一次阶梯小值时不得降下来。"""
        self.prober._note_rate_limit(
            "h3", "bulk probe guard: ip 9.9.9.9 requested 2 distinct models in 60s")
        high = self.prober._host_gap["h3"]
        self.assertGreater(high, Prober._BACKOFF_LADDER[0])
        self.assertFalse(self.prober._note_rate_limit("h3", "429 slow down"))
        self.assertEqual(self.prober._host_gap["h3"], high)

    def test_backoff_is_per_host(self):
        """一个站退避不影响别的站 —— 否则一次偶发 429 拖慢整批。"""
        self.prober._note_rate_limit("ha", "429")
        self.assertIn("ha", self.prober._host_gap)
        self.assertNotIn("hb", self.prober._host_gap)

    def test_learned_value_is_capped(self):
        """站方报出荒谬窗口时按上限收口（原有行为）。"""
        self.prober._note_rate_limit(
            "h4", "requested 1 distinct model in 36000s")
        self.assertEqual(self.prober._host_gap["h4"], Prober._MAX_LEARNED_GAP)


class RateSignalCoverageTests(unittest.TestCase):
    """429 与「限流」必须和「限频」一样触发学习。

    不直接跑 `_call`（那要真发请求），而是钉住判定条件本身：三类信号
    任一命中即学习，而「临时」类里的真瞬时故障（000、连接中断）不学。
    """

    def _should_learn(self, category: str, status: str) -> bool:
        # 与 pipeline._call 末尾那个判定保持同一形状。
        return category in ("限频", "限流") or status == "429"

    def test_three_rate_signals_learn(self):
        self.assertTrue(self._should_learn("限频", "403"))
        self.assertTrue(self._should_learn("限流", "429"))
        self.assertTrue(self._should_learn("临时", "429"))

    def test_transient_non_rate_failures_do_not_learn(self):
        """000 / 连接中断是真故障，不是「你太快了」—— 不该让节拍退避。"""
        self.assertFalse(self._should_learn("临时", "000"))
        self.assertFalse(self._should_learn("未知", "500"))
        self.assertFalse(self._should_learn("客户端", "401"))


class SiteModelFactReuseTests(unittest.TestCase):
    """站方属性跨凭据复用一次实测；凭据相关结论不受影响。"""

    def setUp(self):
        self.prober = _prober()
        self.events: list[tuple[str, dict]] = []
        self.prober.on_event = lambda name, payload: self.events.append((name, payload))

    def _verdict(self, host="agentrouter.test", section="codex-api-key",
                 model="gpt-6-astra"):
        from cpa_probe.pipeline import SectionVerdict
        v = SectionVerdict(section=section, base_url=f"https://{host}/v1")
        v.models = [model]
        v.usable = True
        return v

    def _row(self, key):
        from cpa_probe.parse import ParsedRow
        # line_no / raw 是必填位置参数；raw 用脱敏占位，测试不该带真实凭据形态。
        return ParsedRow(line_no=1, raw="<test row>",
                         bare="https://agentrouter.test", api_key=key)

    def test_swap_is_probed_once_per_site_section_model(self):
        calls = []

        def fake_call(section, base, key, model, **kw):
            calls.append(key)
            from cpa_probe.pipeline import Attempt
            # category / action / elapsed_ms 也是必填位置参数。
            return Attempt(section=section, model=model, combo=kw.get("combo", ""),
                           status="200", category="可用", action="", elapsed_ms=1)

        with patch.object(self.prober, "_call", fake_call), \
             patch.object(pipeline.fingerprint, "swap_rate",
                          lambda samples: {"swap": False, "rate_pct": 0}):
            first = self._verdict()
            self.prober._stage4_swap(self._row("key-one"), first)
            n_after_first = len(calls)
            self.assertGreater(n_after_first, 0, "第一把 Key 必须真实测")

            second = self._verdict()
            self.prober._stage4_swap(self._row("key-two"), second)
            self.assertEqual(len(calls), n_after_first,
                             "第二把 Key 不得为同站同段同模型重复发请求")

        self.assertEqual(second.swap, first.swap)
        self.assertIn("swap-reused", [n for n, _ in self.events])

    def test_context_window_is_probed_once_per_site_section_model(self):
        """最贵的一项：每段 4-6 次最大约 110 万字符的大 body 请求。"""
        bisects = []

        def fake_bisect(row, v, model):
            bisects.append(row.api_key)
            return 900000, False

        with patch.object(self.prober, "_bisect", fake_bisect):
            first = self._verdict()
            self.prober._stage4_context(self._row("key-one"), first)
            second = self._verdict()
            self.prober._stage4_context(self._row("key-two"), second)

        self.assertEqual(bisects, ["key-one"], "窗口只该二分一次")
        self.assertEqual(second.max_context_length, 900000)
        self.assertFalse(second.context_untrusted)
        self.assertIn("context-reused", [n for n, _ in self.events])

    def test_different_model_is_not_reused(self):
        """换模型就不是同一个事实 —— 窗口按模型配，不得外推。"""
        bisects = []
        with patch.object(self.prober, "_bisect",
                          lambda row, v, model: (bisects.append(model), (1000, False))[1]):
            self.prober._stage4_context(self._row("k"), self._verdict(model="m1"))
            self.prober._stage4_context(self._row("k"), self._verdict(model="m2"))
        self.assertEqual(bisects, ["m1", "m2"])

    def test_different_host_is_not_reused(self):
        bisects = []
        with patch.object(self.prober, "_bisect",
                          lambda row, v, model: (bisects.append(v.base_url), (1000, False))[1]):
            self.prober._stage4_context(self._row("k"), self._verdict(host="a.test"))
            self.prober._stage4_context(self._row("k"), self._verdict(host="b.test"))
        self.assertEqual(len(bisects), 2)

    def test_cache_is_instance_scoped_not_global(self):
        """缓存只在一轮任务内有效 —— 站方随时可能改配置。"""
        other = _prober()
        self.assertIsNot(self.prober._site_model_facts, other._site_model_facts)
        self.assertEqual(other._site_model_facts, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
