"""One key's client-identity refusal must not stop its sibling credentials."""
# 身份档拒绝的凭据隔离回归（2026-10-12 本机实测，用户第 7 条「一个 KEY 不能
# 拖垮全部」）
# ==========================================================================
# 现场：`accounts.txt` 里同一主机 10 把不同 Key，第一把的**推理**请求回
# HTTP 401、正文含 `unauthorized client detected`。该正文被 classify 归入
# 「客户端」（处置是「补客户端标识后重试」，见 classify.py 里 2026-09-17
# 从「鉴权」挪过来的说明），而 `probe_policy.finish` 把它升级成
# `provider_restriction` 的**全站**永久停止 —— 账本里只剩 1 个凭据哈希，
# 另外 9 把 Key 一次都没被预留（usable=false、category=未实测、
# probe_policy_code=provider_stopped、attempts=0），界面 10 个候选 0 个
# 可用段、0 个方案、所有参数空白。
#
# 同主机反证（section_gate.py docstring，2026-10-09）：裸 `x-api-key` 回
# `401 unauthorized client detected`，而带 claude-cli UA + x-app 头回
# `200`。这句话说的是**请求形态**被拒，不是站方禁测 —— 正是身份画像梯
# （profiles.py）存在的意义。全站永久停止会把梯子在**第一档**掐死
# （pipeline._stage1 的 _try_profiles 永远拿不到后续档位）。
#
# 本文件把修复钉住（修法见 probe_policy._apply_result 里的作用域修正）：
#   1. 推理请求收到「客户端」类拒绝 → 仅该预留如实记账，其余凭据照常
#      可预留、可结算（修复前此用例第一条断言就红）；
#   2. 同一凭据也仍可继续预留 —— 画像梯对同一个 Key 逐档重试，
#      所以这里既不能全站停、也不能按凭据停；
#   3. 目录请求收到同样正文仍按既有契约产出 1 条证据 + 全站停止
#      （test_catalog_restriction_scope 的刻意契约，不得回退 —— 之前
#      一次「全量豁免目录请求」的改法打红过 4 个套件的 11 条断言）；
#   4. 推理请求上的真·全站拒绝（`account suspended` 这类 _RESTRICTION
#      命中的正文）与 kind=unknown 的调用点仍全站永久停止，
#      fail-closed 不被削弱。
import sqlite3
import unittest
from contextlib import closing

import test_probe_policy as fixtures
from test_probe_policy import URL, HEADERS, configuration

# 现场 401 的正文形态（与 test_pipeline.py 的 gatebroke 画像同款）。
LIVE_401_BODY = '{"error":{"message":"unauthorized client detected"}}'


class CredentialScopeIsolationTests(unittest.TestCase):
    setUp = fixtures.ProbePolicyTests.setUp
    engine = fixtures.ProbePolicyTests.engine
    deny = fixtures.ProbePolicyTests.deny

    def inference(self, engine, headers, status, body):
        """按真实调用点的语义发一次**推理**请求。

        pipeline._send 用 `request_kind="inference"` 装饰（pipeline.py
        里 `_call` 的那次 `_send`），预留与结算都发生在同一个上下文里；
        kind 会随预留持久化进 reservation_requests，结算的作用域判定
        以它为准。
        """
        token = self.module._REQUEST_KIND.set('inference')
        self.addCleanup(self.module._REQUEST_KIND.reset, token)
        permit = engine.reserve(URL, headers)
        self.assertTrue(permit.allowed, permit.reason)
        self.assertTrue(engine.finish(permit, status, body))
        return permit

    def ledger_facts(self):
        with closing(sqlite3.connect(self.ledger)) as db:
            return {
                'credentials': db.execute(
                    "SELECT COUNT(DISTINCT credential) FROM reservations").fetchone()[0],
                'evidence': db.execute(
                    "SELECT COUNT(*) FROM restriction_evidence").fetchone()[0],
                'stopped': db.execute("SELECT stopped FROM providers").fetchone()[0],
                'outcomes': [row[0] for row in
                             db.execute("SELECT outcome FROM reservations ORDER BY rowid")],
            }

    def test_one_key_client_refusal_does_not_stop_sibling_credentials(self):
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        first = {'Authorization': 'Bearer synthetic-credential-one'}
        second = {'Authorization': 'Bearer synthetic-credential-two'}
        third = {'x-api-key': 'synthetic-credential-three'}
        self.inference(engine, first, '401', LIVE_401_BODY)
        # 修复前：第一条预留就把 providers.stopped 写成 provider_restriction，
        # 下面两次预留全部回 provider_stopped —— 另外两把 Key 一次都发不出去。
        for headers in (second, third):
            permit = engine.reserve(URL.replace('/models', '/responses'), headers)
            self.assertTrue(permit.allowed, permit.reason)
            self.assertTrue(engine.finish(permit, '200', '{"ok":true}'))
        # 同一凭据也必须还能继续预留：画像梯对同一个 Key 逐档升级
        # （_try_profiles 每一档用的都是同一个 row.api_key）。
        self.assertTrue(engine.check(URL.replace('/models', '/responses'), first).allowed)
        facts = self.ledger_facts()
        # 2026-10-12 现场是「10 把 Key 只剩 1 个凭据哈希」；修复后三条预留
        # 必须留下三个不同的凭据哈希 —— 一个 KEY 的失败不得吞掉其余 KEY。
        self.assertEqual(3, facts['credentials'])
        # 不再产生全站证据，也不再写全站停止。
        self.assertEqual(0, facts['evidence'])
        self.assertEqual('', facts['stopped'])
        # 预留级审计仍如实记录这条响应属于限制类（只是不再升级成全站结论）：
        # 预算照常消耗、outcome 非空，结算幂等性（_replay 认 outcome）不破。
        self.assertEqual(['provider_restriction', '200', '200'], facts['outcomes'])

    def test_catalog_request_keeps_the_deliberate_provider_wide_verdict(self):
        """同一正文在**目录**请求上仍产出 1 条证据 + 全站停止。

        目录 GET 是项目按固定最小身份（cc-min + x-app，见
        profiles.catalog_identity_headers）主动发的；此时仍回「客户端」类
        拒绝，按显式拒绝记账 —— 这是 test_catalog_restriction_scope 的
        刻意契约。本用例防止将来有人把「推理侧豁免」扩大成「目录侧也
        豁免」：目录侧的判定不许跟着改。
        """
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        token = self.module._REQUEST_KIND.set('model_catalog')
        self.addCleanup(self.module._REQUEST_KIND.reset, token)
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed, permit.reason)
        self.assertTrue(engine.finish(permit, '401', 'unauthorized client detected'))
        facts = self.ledger_facts()
        self.assertEqual(1, facts['evidence'])
        self.assertEqual('provider_restriction', facts['stopped'])
        self.deny(engine.check(URL, {'Authorization': 'Bearer synthetic-other'}),
                  'provider_stopped')

    def test_genuine_site_wide_refusal_on_inference_still_stops_every_credential(self):
        """推理请求上的真·全站拒绝（_RESTRICTION 命中的正文）语义不变。

        `account suspended` 是全站拒绝的措辞，不是请求形态问题 ——
        修复只针对「客户端」类身份档，这条必须仍把整个服务商永久停掉，
        否则 fail-closed 就被削弱了。
        """
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        self.inference(engine, HEADERS, '403', 'account suspended')
        facts = self.ledger_facts()
        self.assertEqual(1, facts['evidence'])
        self.assertEqual('restriction', facts['stopped'])
        self.deny(engine.check(URL, {'Authorization': 'Bearer synthetic-other'}),
                  'provider_stopped')

    def test_unknown_request_kind_keeps_the_fail_closed_default(self):
        """没有语义调用点证据的请求（kind=unknown，例如 WS 握手、写回验证）
        维持原判：客户端拒绝仍按全站停止处理。

        修复只认「预留时持久化的 kind == inference」这一条 —— 语义调用点
        证据（policy_request_kind 的注释：never inferred from a
        user-controlled URL）。放宽范围必须先有同等级的实测证据。
        """
        engine = self.engine(configuration(max_requests=100, max_requests_per_credential=100))
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed, permit.reason)
        self.assertTrue(engine.finish(permit, '401', 'unauthorized client detected'))
        facts = self.ledger_facts()
        self.assertEqual(1, facts['evidence'])
        self.assertEqual('provider_restriction', facts['stopped'])
        self.deny(engine.check(URL, {'Authorization': 'Bearer synthetic-other'}),
                  'provider_stopped')


if __name__ == '__main__':
    unittest.main()
