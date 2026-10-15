"""云厂商 WAF 拦截页必须判 WAF，不能判「临时」—— 2026-10-15 本机实测加。

现场
----
golf.example 在 2026-10-15 这几小时内上了阿里云 WAF。拦截页回的是
**HTTP 200**，正文是 HTML，里面**没有任何中文拦截字样**，只有厂商标记：

    <meta name="aliyun_waf_aa" content="…">
    <meta name="aliyun_waf_bb" content="…">
    appkey: "CF_APP_WAF",  // 应用标识

原有的 WAF 规则只认中文（`访问已被拦截|安全验证|人机验证`），一个都匹配不上，
于是落到状态码兜底：整页 HTML 的 200 被判「临时」—— 而「临时」的处置是
**可重试 + 该试代理**。

实测代价（同一轮 77 次请求）：11 次判「临时」、2 次白试代理与重试。而
`pipeline._PROXY_POINTLESS` 里「WAF」那条早就写明「认客户端形态而非 IP，
实测三段都配了 mihomo 仍返回同一个拦截页」。2026-10-15 复测完全一致：

    直连       -> 200 HTML/WAF（aliyun_waf_aa）
    过 mihomo  -> 200 HTML/WAF（同一个拦截页）

方向错比多跑几次更坏：判「临时」让操作员以为「稍后能好」，而真正该做的是
换请求形态或等站方放行。

本文件钉四条不变式：
  1. 厂商标记（aliyun_waf / CF_APP_WAF / …）判 WAF，哪怕状态码是 200；
  2. WAF 的处置是「不重试、不换 IP」—— 这决定了上面那条分类的全部价值；
  3. 不认泛泛的 "waf" 子串：正常响应里偶然出现（模型名、用户内容）不得误判；
  4. 原有的中文拦截页规则与 CF 挑战（IP封，换 IP 有效）语义不变。

不发任何网络请求。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe.classify import DISPOSITION, classify

# 现场原文（截断保留结构，不含任何真实域名或凭据）
ALIYUN_PAGE = (
    '<!doctype html> <meta charset="UTF-8"> '
    '<meta name="aliyun_waf_aa" content="ff926c7f07e45e2e487a29a6197d3460"> '
    '<meta name="aliyun_waf_bb" content="eade71455e2ad9c6d08b82bc7d98df8c">'
)
CF_APP_WAF_JS = (
    'appkey: "CF_APP_WAF", // 应用标识'
    'var AC_Opt = {userUserId: requestInfo.userUserId TraceID: 0a0'
)


class VendorWafDetectionTests(unittest.TestCase):
    def test_aliyun_marker_on_a_200_is_waf(self):
        """状态码是 200 也要判 WAF —— 拦截页就是这么回的。"""
        category, why = classify("200", ALIYUN_PAGE)
        self.assertEqual("WAF", category, why)

    def test_cf_app_waf_marker_is_waf(self):
        category, why = classify("200", CF_APP_WAF_JS)
        self.assertEqual("WAF", category, why)

    def test_markers_are_detected_regardless_of_status(self):
        """同一个拦截页可能带 200 / 403 / 503，分类不该跟着状态码摇摆。"""
        for status in ("200", "403", "429", "503"):
            with self.subTest(status=status):
                self.assertEqual("WAF", classify(status, ALIYUN_PAGE)[0])

    def test_other_vendor_markers(self):
        for marker in ("tencent_waf", "x-waf-block: 1", "huawei_waf",
                       "baidu_waf", "safeline", "modsecurity"):
            with self.subTest(marker=marker):
                self.assertEqual("WAF", classify("200", "blocked " + marker)[0])

    def test_waf_disposition_means_no_retry_and_no_proxy(self):
        """这条分类的全部价值在处置上：重试和换 IP 都没用。"""
        retryable, proxy_helps, _advice = DISPOSITION["WAF"]
        self.assertFalse(retryable, "WAF 被标成可重试 —— 会白烧请求")
        self.assertFalse(proxy_helps, "WAF 被标成换 IP 有效 —— 实测代理同样被拦")

    def test_plain_waf_substring_is_not_enough(self):
        """只认厂商专有标记名。正常响应里偶然出现 waf 不得误判。"""
        for body in ('{"data":[{"id":"waffle-v1"}]}',
                     '{"model":"gpt-waffle","content":"ok"}',
                     '{"content":"the word waf appears here"}'):
            with self.subTest(body=body):
                self.assertNotEqual("WAF", classify("200", body)[0])

    def test_clean_json_200_is_not_waf(self):
        self.assertNotEqual("WAF", classify("200", '{"data":[{"id":"gpt-6-astra"}]}')[0])

    # ---- 既有语义不得被这次改动波及 ----

    def test_chinese_block_page_still_waf(self):
        for body in ("访问已被拦截", "请完成安全验证", "需要人机验证"):
            with self.subTest(body=body):
                self.assertEqual("WAF", classify("403", body)[0])

    def test_cf_challenge_still_ip_scoped(self):
        """CF 挑战是按 IP 声誉，换 IP **有效** —— 与 WAF 处置相反，不能混。"""
        for body in ("challenge-platform", "cf-mitigated: challenge",
                     "just a moment"):
            with self.subTest(body=body):
                category = classify("403", body)[0]
                self.assertEqual("IP封", category)
        self.assertTrue(DISPOSITION["IP封"][1], "IP封 应当是换代理有效")

    def test_transient_upstream_errors_unchanged(self):
        """503/429 仍是「临时」—— 它们表示请求已过门禁、上游忙。"""
        for status in ("503", "429"):
            with self.subTest(status=status):
                self.assertEqual("临时",
                                 classify(status, '{"error":{"message":"Service Unavailable"}}')[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
