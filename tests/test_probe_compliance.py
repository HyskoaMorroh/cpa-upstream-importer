"""Synthetic offline probing regressions. Run directly with the pinned Python."""
import concurrent.futures
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cpa_probe import pipeline as p, classify as unused
from cpa_probe import cpa_source_probe as source, client, profiles
from cpa_probe.batch import BatchProber
from cpa_probe.parse import ParsedRow

C = "codex-api-key"
O = "openai-compatibility"
L = "claude-api-key"


def response(section=O, model="gpt-6"):
    if section == C:
        return 'data: ' + json.dumps({"type": "response.completed", "response": {
            "status": "completed", "model": model, "output": [{
                "type": "message", "content": [{"type": "output_text", "text": "TCP"}]}]}}) + "\n\n"
    if section == L:
        return json.dumps({"model": model, "content": [{"type": "text", "text": "TCP"}],
                           "stop_reason": "end_turn"})
    return json.dumps({"model": model, "choices": [
        {"message": {"content": "TCP"}, "finish_reason": "stop"}]})


class ProbeCompliance(unittest.TestCase):
    def setUp(self):
        self.ident = source.CpaIdentity()
        self.stack = __import__("contextlib").ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(source, "cached_identity", return_value=self.ident))
        self.stack.enter_context(patch.object(client, "send", side_effect=AssertionError("unmocked HTTP")))
        self.stack.enter_context(patch.object(client, "probe_proxy", return_value=(True, "fixture")))
        self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("external socket")))

    def prober(self, **kw):
        return p.Prober(gap=0, workers=1, swap_samples=0, probe_context=False,
                        probe_capabilities=False, **kw)

    def row(self, base="https://unit.example/a", key="fixture-one"):
        return ParsedRow(1, "", base, key)

    def test_invalid_responses_never_ok(self):
        for body in ("", "{}", "[]", "ready", "<html>ready</html>",
                     '{"error":{"message":"bad"}}',
                     'data: {"type":"error","error":{"message":"bad"}}\n\n',
                     'data: {"type":"response.created"}\n\n'):
            with self.subTest(body=body), patch.object(client, "send", return_value=client.Response("200", body, 1)):
                att = self.prober()._call(C, self.row().bare, "fixture", "gpt-6", combo="test")
                self.assertFalse(att.ok)

    def test_transport_error_never_ok(self):
        with patch.object(client, "send", return_value=client.Response("200", response(), 1, "decode failed")):
            self.assertFalse(self.prober()._call(O, self.row().bare, "fixture", "gpt-6", combo="test").ok)

    def test_codex_requires_stream(self):
        with patch.object(client, "send", return_value=client.Response("200", response(), 1)):
            self.assertFalse(self.prober()._call(C, self.row().bare, "fixture", "gpt-6", combo="test").ok)

    def test_second_key_error_does_not_inherit(self):
        shape = p.SectionVerdict(O, self.row().bare, usable=True,
                                 models=["gpt-6", "gpt-6-sol"], max_context_length=275000)
        with patch.object(client, "send", return_value=client.Response("200", '{"error":{"message":"bad"}}', 1)):
            v = self.prober()._reuse_shape(self.row(key="fixture-two"), O, shape)
        self.assertFalse(v.usable)
        self.assertEqual(v.models, [])
        self.assertIsNone(v.max_context_length)

    def test_context_error_not_trusted(self):
        v = p.SectionVerdict(O, self.row().bare, usable=True, models=["gpt-6"])
        with patch.object(client, "send", return_value=client.Response("200", '{"error":{"message":"bad"}}', 1)):
            limit, _ = self.prober()._bisect(self.row(), v, "gpt-6")
        self.assertIsNone(limit)

    def test_cache_isolates_paths_but_reuses_site_level_failures(self):
        """路径不同要各探各的；同路径的第 2 把 Key 复用**站+段级**结论。

        两条轴的判据不同，不能混成一条（2026-09-12 厘清）：

          · **路径**轴 —— 同一主机按路径挂的是互不相干的上游
            （假上游脚本就是 `/good` 与 `/gate`）。形态与失败结论都是
            「这个上游」的属性，`/a` 的一次门禁不该让 `/b` 连探都不探。
            这一条原来是坏的：single-flight 的键用 `row.host`，路径被抹掉。
          · **Key** 轴 —— 门禁 / WAF / IP封 / 死路 / 时段这些拒绝与凭据无关
            （见 `_HOST_LEVEL_FAIL` 与 `_reuse_dead`），再问一遍答案一样。
            同路径的第 2..N 把 Key 零请求复用是**有意的**：实测 15 个 Key
            挂同一主机且该段不通时，从 15 次完整探测降到 1 次 + 14 次复用，
            而且那 15 次因为门闩在是严格串行的。

        所以三行输入（同路径两把 Key + 另一路径一把）应当只触发 **2** 次
        完整探测：`/a` 一次、`/b` 一次。断言 3 会把上面那条省钱路径拆掉。
        """
        prober = self.prober()
        calls = []
        def full(row, sec):
            calls.append((row.bare, row.api_key))
            return p.SectionVerdict(sec, row.bare, category="门禁")
        with patch.object(prober, "_full_probe", side_effect=full):
            for row in (self.row(), self.row(key="fixture-two"), self.row(base="https://unit.example/b")):
                prober._probe_one_section(row, O)
        self.assertEqual([b for b, _k in calls],
                         ["https://unit.example/a", "https://unit.example/b"])
        # 凭证级的失败仍然逐 Key 各判各的 —— 那是 Key 自己的属性
        self.assertNotIn("鉴权", p.Prober._HOST_LEVEL_FAIL)
        self.assertNotIn("余额", p.Prober._HOST_LEVEL_FAIL)

    def test_existing_origin_headers_proxy_and_snapshot(self):
        cfg = {O: [{"base-url": "https://unit.example", "headers": {"X-Required": "yes"},
                    "api-key-entries": [{"api-key": "fixture-one", "proxy-url": "http://proxy.example:8080"}]}]}
        prober = self.prober(cfg_snapshot=cfg)
        seen = []
        def send(url, **kw):
            seen.append((url, kw))
            return client.Response("200", response(), 1)
        with patch.object(client, "send", side_effect=send):
            prober._call(O, "https://unit.example", "fixture-one", "gpt-6", combo="test")
        self.assertEqual(seen[0][0], "https://unit.example/chat/completions")
        self.assertEqual(seen[0][1]["headers"]["X-Required"], "yes")
        self.assertEqual(seen[0][1]["proxy"], "http://proxy.example:8080")

    def test_beta_replay_keeps_body_and_headers(self):
        prober = self.prober()
        top = next(x for x in profiles.ladder(L, source_identity=self.ident) if x.body_patch)
        v = p.SectionVerdict(L, profile_name=top.name + "+beta",
                             min_headers={"anthropic-beta": "fixture-beta"})
        kw = prober._profile_kwargs(v, "fixture-one")
        self.assertTrue(kw.get("body_patch"))
        self.assertIn("fixture-beta", kw["extra_headers"]["anthropic-beta"])

    def test_baseline_reaches_third_candidate(self):
        prober = self.prober()
        def send(url, **kw):
            model = json.loads(kw["body"])["model"]
            return client.Response("200", response(O, model), 1) if model == "gpt-6-c" else client.Response("404", "model_not_found", 1)
        with patch.object(prober, "_stage0_catalog", return_value=["gpt-6-a", "gpt-6-b", "gpt-6-c"]), \
             patch.object(client, "send", side_effect=send):
            v = prober._stage1(self.row(), O)
        self.assertIn("gpt-6-c", v.models)

    def test_same_generation_peers_come_from_topup_not_the_probe_budget(self):
        """同代变体要全勾上，但那是**写回清单**那一层的事，不是探测预算。

        2026-09-12 把这一项挪到正确的层。原来它要求 `_stage2` 在
        `max_models=1` 下仍然收满 6 个 —— 那等于要求探测**忽略自己的预算**。

        两个上限各管各的（见 `MAX_MODELS_PER_SECTION` 的说明）：
          · `max_models` —— 每段最多**验**几个模型。每验一个就发一次推理
            请求，79 个凭据规模下这是主要成本，也是撞站方限频的主因。
          · 写进 config.yaml 几个 —— 不发请求，多写几行只是让 CPA 的模型
            注册表多几项。

        docx 第 4 条要的「勾了 gpt-5.6 却没勾 gpt-5.6-sol 是重大失误」由
        `model_catalog.topup_to_market_top` 在**定方案**时补齐：拿实测到的
        那一个去比市面名录，把同产品线同世代的变体全部带上。所以探测只花
        1 次请求，而落盘清单仍然是完整的一代。
        """
        prober = self.prober(max_models=1, max_model_attempts=8)
        names = ["gpt-6-" + x for x in "abcdef"]
        v = p.SectionVerdict(O, self.row().bare, usable=True, models=names[:1], catalog=names)
        def send(url, **kw):
            return client.Response("200", response(O, json.loads(kw["body"])["model"]), 1)
        with patch.object(client, "send", side_effect=send):
            prober._stage2(self.row(), v)
        # 探测守住预算：只验了 1 个
        self.assertEqual(v.models, names[:1])
        # 而补齐那一层把同代变体全带上 —— 这才是「不许漏勾」的落点
        from cpa_probe import model_catalog as mc
        merged, added, _src = mc.topup_to_market_top(O, v.models, remote=names)
        self.assertEqual(set(merged), set(names))
        self.assertEqual(set(added), set(names[1:]))

    def test_mid_system_503_unknown(self):
        v = p.SectionVerdict(L, self.row().bare, usable=True, models=["claude-opus-5"])
        with patch.object(client, "send", return_value=client.Response("503", "busy", 1)):
            self.prober()._probe_mid_system(self.row(), v)
        self.assertIsNone(v.rebuild_mid_system)

    def test_ws_handshake_states(self):
        """101 = 支持；连接层失败与 5xx/429 = 判不了；其余 = 不支持。

        2026-09-12 改。这一项原来断言 101 也要判 None，那是错的：101 就是
        握手成功本身（`_probe_websockets` 与 CPA 的
        codex_websockets_connection.go 同构），没有比它更直接的证据。

        真正该判 None 的是**站方此刻不行**那一类 —— 见 `Prober._is_transient`：
        5xx / 429 说的是过载或限频，与「支不支持 WS」无关。判成 False 的代价
        是实的：`websockets` 抄错方向后 CPA 走 WS 通道握手失败**不会回落
        HTTP**（codex_websockets_executor.go:71-77），那个凭据的 WS 请求全废。
        """
        cases = [("101", True), ("000", None), ("503", None), ("429", None),
                 ("400", False), ("404", False)]
        for status, want in cases:
            with self.subTest(status=status):
                v = p.SectionVerdict(C, self.row().bare, usable=True, models=["gpt-6"])
                with patch.object(client, "ws_handshake",
                                  return_value=client.Response(status, "", 1)):
                    self.prober()._probe_websockets(self.row(), v)
                self.assertIs(v.websockets, want)
                self.assertTrue(v.websockets_note, "结论必须带实测依据")

    def test_batch_cancellation_propagates(self):
        prober = self.prober()
        with patch.object(prober, "probe", side_effect=concurrent.futures.CancelledError):
            with self.assertRaises(concurrent.futures.CancelledError):
                BatchProber(prober, 1).probe_batch([self.row(), self.row(key="fixture-two")])


if __name__ == "__main__":
    unittest.main()
