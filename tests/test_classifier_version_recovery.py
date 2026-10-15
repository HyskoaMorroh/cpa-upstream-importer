"""旧版分类器的限制证据可以有界复核；当前版的证据语义不变 —— 2026-10-13 加。

现场（本机实测）
--------------
`pipeline._stage0_catalog` 修复前，分段目录 GET 不带身份头
（`request.models_endpoint` 只放凭据），站方回
`401 unauthorized client detected`，`classify` 归「客户端」，
`finish` 升级成 `provider_restriction` 全站永久停止，并写一行
`restriction_evidence`。

修复后同一个目录请求带上身份头，同一把 Key、同一 URL 回 200 + 真实目录
（直连与过代理结果一致，与代理无关）。也就是说那条 401 记录的是
**我们自己发错了请求形态**，不是站方拒绝。

但旧账本不会自愈：`restriction_evidence` 一旦有行，`_legacy_recovery`
就返回 False、`_assess` 直接判 `provider_stopped`。实测新镜像起来后
账本里 2 条证据（401 + 403）让 2 个 provider 永久停止，日志里是
`prescan-miss status=blocked probe_policy_code=provider_stopped`，
一个请求都发不出去 —— **光改代码救不回已经被判死的站**。

修法是让 `classifier_version` 做它本该做的事（该列从引入起一直写死 1、
从未被任何查询读过）：版本 < `CLASSIFIER_VERSION` 的证据不再单独构成永久
封锁，可以走**已有的**那条有界复核路径；当前版本的证据语义完全不变。

本文件钉五条不变式：
  1. 当前版本写入的证据仍然永久停止（不得被这次放宽波及）；
  2. 旧版本（version < 当前）的证据允许在显式任务作用域下有界复核；
  3. 复核不是放行 —— 没有显式作用域时仍然停止；
  4. 旧证据行本身不被删除或改写（审计链完整）；
  5. 版本号单调不减，且写入值就是 `CLASSIFIER_VERSION`。

全部离线：只用合成 URL 与临时账本，不发任何网络请求。
"""
import sqlite3
import unittest
from contextlib import closing

import test_probe_policy as fixtures
from test_probe_policy import URL, HEADERS, configuration


class ClassifierVersionRecoveryTests(unittest.TestCase):
    setUp = fixtures.ProbePolicyTests.setUp
    engine = fixtures.ProbePolicyTests.engine

    # ---- helpers ----------------------------------------------------------

    def settle_catalog(self, status, body):
        """以目录请求的身份结算一次限制响应，产出当前版本的证据。"""
        token = self.module._REQUEST_KIND.set('model_catalog')
        self.addCleanup(self.module._REQUEST_KIND.reset, token)
        engine = self.engine(configuration(max_requests=100,
                                           max_requests_per_credential=100))
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed, permit.reason)
        self.assertTrue(engine.finish(permit, status, body))
        return engine

    def evidence_rows(self):
        with closing(sqlite3.connect(self.ledger)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                'SELECT classifier_version, status, reason FROM restriction_evidence')]

    def downgrade_evidence(self, version=1):
        """把账本里的证据改成旧版本 —— 模拟修复前镜像留下的行。"""
        with closing(sqlite3.connect(self.ledger)) as db:
            db.execute('UPDATE restriction_evidence SET classifier_version=?', (version,))
            db.commit()

    # ---- contracts --------------------------------------------------------

    def test_written_version_is_the_current_classifier_version(self):
        """不再写死 1，否则版本机制等于不存在。"""
        self.settle_catalog('401', 'unauthorized client detected')
        rows = self.evidence_rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(self.module.CLASSIFIER_VERSION,
                         rows[0]['classifier_version'])

    def test_classifier_version_is_at_least_two(self):
        """1 是「从未区分过」的历史值；放宽旧证据要求当前版本高于它。"""
        self.assertGreaterEqual(self.module.CLASSIFIER_VERSION, 2)

    def test_current_version_evidence_still_stops_permanently(self):
        """本轮分类器的结论不受这次放宽影响 —— 与
        test_catalog_restriction_scope / test_credential_scope_isolation
        钉的契约同向。"""
        engine = self.settle_catalog('401', 'unauthorized client detected')
        self.assertEqual(engine.check(URL, HEADERS).code, 'provider_stopped')
        # 换个任务作用域也不行：当前版本的证据就是永久结论。
        self.module._JOB_SCOPE.set('explicit-reprobe')
        self.addCleanup(self.module._JOB_SCOPE.set, '')
        self.assertEqual(self.engine().check(URL, HEADERS).code, 'provider_stopped')

    def test_legacy_version_evidence_becomes_reviewable(self):
        """旧版证据不再单独判死 —— 这正是「救不回已判死的站」那个缺陷。"""
        self.settle_catalog('401', 'unauthorized client detected')
        self.downgrade_evidence(1)
        self.module._JOB_SCOPE.set('explicit-reprobe')
        self.addCleanup(self.module._JOB_SCOPE.set, '')
        self.clock.return_value = 10000
        code = self.engine().check(URL, HEADERS).code
        self.assertNotEqual('provider_stopped', code,
                            '旧版证据仍在永久封锁，复核路径没通')

    def test_legacy_recovery_still_requires_an_explicit_job_scope(self):
        """放宽不等于放行：没有显式任务作用域时照旧停止。"""
        self.settle_catalog('401', 'unauthorized client detected')
        self.downgrade_evidence(1)
        self.clock.return_value = 10000
        # 不设 _JOB_SCOPE —— 默认作用域下 `_assess` 的 `not _JOB_SCOPE.get()`
        # 分支仍然判死。
        self.assertEqual(self.engine().check(URL, HEADERS).code,
                         'provider_stopped')

    def test_legacy_rows_are_never_deleted_or_rewritten(self):
        """审计链必须完整（test_new_classifier_does_not_erase_existing_
        evidence 的同一条精神）：只是不再单独判死，不是擦掉。"""
        self.settle_catalog('401', 'unauthorized client detected')
        self.downgrade_evidence(1)
        before = self.evidence_rows()
        self.module._JOB_SCOPE.set('explicit-reprobe')
        self.addCleanup(self.module._JOB_SCOPE.set, '')
        self.clock.return_value = 10000
        self.engine().check(URL, HEADERS)
        self.assertEqual(before, self.evidence_rows(), '旧证据被改动了')

    def test_genuine_site_wide_refusal_is_unaffected_by_version(self):
        """真·全站拒绝（_RESTRICTION 命中的措辞）走的是 `restriction`
        而不是 `provider_restriction`，与版本放宽完全无关。"""
        engine = self.settle_catalog('401', 'account suspended')
        self.assertEqual(engine.check(URL, HEADERS).code, 'provider_stopped')
        self.downgrade_evidence(1)
        self.module._JOB_SCOPE.set('explicit-reprobe')
        self.addCleanup(self.module._JOB_SCOPE.set, '')
        self.clock.return_value = 10000
        # stopped 不是 provider_restriction，`_legacy_recovery` 第一个条件
        # 就不成立 —— 仍然永久停止。
        self.assertEqual(self.engine().check(URL, HEADERS).code,
                         'provider_stopped')


if __name__ == '__main__':
    unittest.main(verbosity=2)
