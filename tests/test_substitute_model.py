"""站方在失败正文里指名替代模型时，必须按它的指示重打 —— 2026-10-14 加。

现场（本机实测，alfa.example claude 段，5 把真实 Key）
----------------------------------------------------
目录报了 12 个 claude 名字，而请求排序第一的 `claude-opus-5-5` 回 400，
正文是一句完整的使用说明：

    由于 claude 模型供应难以保证，上线 gpt-6-astra-cc-format 模型，
    支持 messages 格式，通过 claude --model 'gpt-6-astra-cc-format[1m]'
    使用该模型

也就是说这个站的 claude 段**没有真正的 claude 模型**，站方改供一个别的
名字，并且把正确用法写清楚了。

修复前的行为（逐跳查实）：
  · 那条正文不命中 `betas.wanted` 的任何一项（它含 `[1m]`，但不含
    「1m 上下文」字样），`classify` 只能归「未知」；
  · 「未知」既不触发画像梯，也不算 `_model_specific_dead_end`，所以
    `spare` 不补候选 —— 这一发基线额度白费；
  · 另一发 `claude-opus-4-7` 回 503，`_BASELINE_MODELS = 2` 用尽；
  · 整段判「未实测」，而站方点名的那个模型**一次都没试过**。目录里
    其实有它，只是排在 claude 族之后，`spare` 的 3 个名额够不到。

实测佐证（同一把 Key、同一端点）：
    claude-opus-5-5           -> 400「上线 gpt-6-astra-cc-format…」
    claude-opus-4-7           -> 400「1m 上下文…请启用」→ 画像梯 id:ctx-1m -> 503
    gpt-6-astra-cc-format     -> 400（裸）/ 429（带 context-1m beta，门禁已过）
    claude-fable-5-1-reversed -> 400（裸）/ 503（带 beta，门禁已过）
`[1m]` 后缀写进 `model` 字段**无效**（三把 Key 一致仍 400）——它是 claude
CLI 的语法糖，HTTP 层真正起作用的只有 `anthropic-beta` 头。

本文件钉五条不变式（全部离线，不发任何网络请求）：
  1. 能从 `--model 'name'` 与「上线 xxx 模型」两种措辞里抽出名字；
  2. `[1m]` 之类的后缀被剥掉，并翻译成等价的 beta 项；
  3. 正文没点名时一个字都不猜（返回空，不得拿正文里别的词去打请求）；
  4. 明显不是模型名的（过短、含路径分隔符）一律拒绝；
  5. `_stage1` 真的会把站方指名的模型加进候选，且只跟随一次。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cpa_probe import betas, cpa_source_probe, pipeline
from cpa_probe.pipeline import Attempt, Prober, SectionVerdict
from cpa_probe.parse import ParsedRow

LIVE_BODY = (
    "由于 claude 模型供应难以保证，上线 gpt-6-astra-cc-format 模型，"
    "支持 messages 格式，通过 claude --model 'gpt-6-astra-cc-format[1m]' 使用该模型"
)


class _Identity:
    snapshot_id = "test"

    def __deepcopy__(self, memo):
        return self


def _prober(**kw):
    with patch.object(cpa_source_probe, "cached_identity", lambda **_: _Identity()):
        return Prober(**kw)


class SubstituteModelExtractionTests(unittest.TestCase):
    def test_live_body_yields_model_and_beta(self):
        """现场那条正文：抽出名字，并把 [1m] 翻成 beta。"""
        model, extra = betas.substitute_model(LIVE_BODY)
        self.assertEqual("gpt-6-astra-cc-format", model)
        self.assertEqual(["context-1m-2025-08-07"], extra)

    def test_suffix_is_stripped_from_the_model_name(self):
        """后缀不能留在 model 字段里 —— 实测带着它仍然 400。"""
        model, _ = betas.substitute_model("use claude --model 'foo-bar[1m]'")
        self.assertEqual("foo-bar", model)
        self.assertNotIn("[", model)

    def test_double_quoted_form_is_accepted(self):
        model, extra = betas.substitute_model('try --model "gpt-7-pro" instead')
        self.assertEqual("gpt-7-pro", model)
        self.assertEqual([], extra)

    def test_chinese_wording_is_accepted(self):
        model, _ = betas.substitute_model("上线 gpt-7-turbo 模型，支持 messages 格式")
        self.assertEqual("gpt-7-turbo", model)

    def test_unknown_suffix_is_dropped_not_guessed(self):
        """只认已知后缀。陌生后缀剥掉但不乱补 beta。"""
        model, extra = betas.substitute_model("--model 'foo-bar[256k]'")
        self.assertEqual("foo-bar", model)
        self.assertEqual([], extra)

    def test_bodies_without_a_named_model_yield_nothing(self):
        """这是关键的「不猜」边界：没点名就一个字都不编。"""
        for body in ('{"error":"1m 上下文已经全量可用，请启用 1m 上下文后重试"}',
                     '{"error":"Service Unavailable"}',
                     "当前 API 不支持所选模型 gpt-5",
                     "", None):
            with self.subTest(body=body):
                self.assertEqual(("", []), betas.substitute_model(body))

    def test_implausible_names_are_rejected(self):
        """过短或含路径分隔符的不是模型名 —— 拒绝，避免白打一次请求。"""
        for body in ("--model 'ab'", "--model '../etc/passwd'",
                     "--model 'a/b'", "--model 'x\\y'"):
            with self.subTest(body=body):
                self.assertEqual(("", []), betas.substitute_model(body))

    def test_existing_beta_hints_are_untouched(self):
        """不得影响原有的 betas.wanted 行为（那条路径已实测走通）。"""
        self.assertEqual(["context-1m-2025-08-07"],
                         betas.wanted("1m 上下文已经全量可用，请启用 1m 上下文后重试"))
        self.assertEqual([], betas.wanted(LIVE_BODY),
                         "现场那条正文不该命中 1m 提示 —— 它靠替代模型路径处理")


class SubstituteModelWiringTests(unittest.TestCase):
    """`_stage1` 必须真的把站方指名的模型加进候选，且只跟随一次。"""

    def setUp(self):
        self.prober = _prober(probe_context=False, probe_capabilities=False,
                              swap_samples=0)
        self.events: list[tuple] = []
        self.prober.on_event = lambda k, d: self.events.append((k, d))
        self.tried: list[str] = []

    def _row(self):
        return ParsedRow(line_no=1, raw="<test>", bare="https://sub.test",
                         api_key="sk-test-only")

    def _fake_call(self, body_for, *, ok_model=None):
        """假 _call：按模型名返回不同正文，记录调用顺序。"""
        def call(section, base, key, model, **kw):
            self.tried.append(model)
            if ok_model and model == ok_model:
                return Attempt(section=section, model=model,
                               combo=kw.get("combo", ""), status="200",
                               category="可用", action="", elapsed_ms=1,
                               response_valid=True, resp_model=model)
            return Attempt(section=section, model=model,
                           combo=kw.get("combo", ""), status="400",
                           category="未知", action="", elapsed_ms=1,
                           excerpt=body_for.get(model, "generic failure"))
        return call

    def _run(self, call, catalog):
        """`_stage1(row, section)` 自己建 base 与 verdict，并先问目录。

        所以这里 patch `_stage0_catalog` 返回受控目录，`_probe_order`
        原样透传（避免排序掺进来成为第二个变量）。
        """
        with patch.object(self.prober, "_call", call), \
                patch.object(self.prober, "_try_profiles", lambda *a, **k: False), \
                patch.object(self.prober, "_stage0_catalog",
                             lambda row, sec, base, **k: list(catalog)), \
                patch.object(self.prober, "_probe_order", lambda s, c: list(c)):
            return self.prober._stage1(self._row(), "claude-api-key")

    def test_named_model_is_probed_even_when_sorted_out_of_reach(self):
        """站方点名的模型即便排在候选窗口之外，也必须被试到。"""
        catalog = ["claude-opus-5-5", "claude-opus-4-7", "x1", "x2", "x3",
                   "x4", "gpt-6-astra-cc-format"]
        call = self._fake_call({"claude-opus-5-5": LIVE_BODY},
                               ok_model="gpt-6-astra-cc-format")
        verdict = self._run(call, catalog)
        self.assertIn("gpt-6-astra-cc-format", self.tried,
                      "站方指名的模型一次都没试 —— 正是修复前的现场")
        self.assertTrue(verdict.usable)
        kinds = [k for k, _d in self.events]
        self.assertIn("substitute-model", kinds)

    def test_substitution_is_followed_only_once(self):
        """替代模型的响应里再提一个名字时不得无限跟随。"""
        chain = {
            "claude-opus-5-5": LIVE_BODY,
            "gpt-6-astra-cc-format": "上线 another-model-name 模型",
        }
        call = self._fake_call(chain)
        self._run(call, ["claude-opus-5-5", "claude-opus-4-7"])
        self.assertIn("gpt-6-astra-cc-format", self.tried)
        self.assertNotIn("another-model-name", self.tried,
                         "跟随了第二层 —— 会变成被正文牵着走的请求放大器")
        self.assertEqual(1, sum(1 for k, _ in self.events
                                if k == "substitute-model"))

    def test_no_substitution_when_body_names_nothing(self):
        """正文没点名时不得多打任何一发。"""
        call = self._fake_call({"a-model": '{"error":"Service Unavailable"}'})
        self._run(call, ["a-model", "b-model"])
        self.assertEqual(["a-model", "b-model"], self.tried)
        self.assertNotIn("substitute-model", [k for k, _ in self.events])

    def test_already_queued_model_is_not_duplicated(self):
        """站方点名的恰好已在候选里时，不重复排队。"""
        call = self._fake_call({"claude-opus-5-5":
                                "上线 claude-opus-4-7 模型"})
        self._run(call, ["claude-opus-5-5", "claude-opus-4-7"])
        self.assertEqual(["claude-opus-5-5", "claude-opus-4-7"], self.tried)


if __name__ == "__main__":
    unittest.main(verbosity=2)
