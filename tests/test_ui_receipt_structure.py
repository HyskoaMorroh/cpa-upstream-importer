"""Receipt nodes must survive collapsing their operation's preview panel."""
from html.parser import HTMLParser
from pathlib import Path
import unittest


class ReceiptParents(HTMLParser):
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
            'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') in ('bmapplymsg', 'tnmsg'):
            self.parents[attrs['id']] = [identifier for _, identifier in self.stack]
        if tag not in self.void:
            self.stack.append((tag, attrs.get('id')))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class ReceiptStructureTests(unittest.TestCase):
    def setUp(self):
        self.tree = ReceiptParents()
        self.tree.feed((Path(__file__).resolve().parents[1] / 'web/index.html').read_text(encoding='utf-8'))
    def test_bulk_receipt_is_outside_collapsible_preview(self):
        self.assertIn('bmapplymsg', self.tree.parents)
        self.assertNotIn('bmpreview', self.tree.parents['bmapplymsg'])

    def test_tuning_receipt_is_outside_collapsible_preview(self):
        self.assertIn('tnmsg', self.tree.parents)
        self.assertNotIn('tnpreview', self.tree.parents['tnmsg'])


class PolicySkippedVerificationTests(unittest.TestCase):
    """跳过的验证不能被算成「通过」，也不能标成红色「失败」。

    2026-10-04 本机真实容器实测：22 项验证全部被策略跳过
    （verify_upstream 的 unbound_gateway 分支），回执标题却写
    「端到端验证：22/22 通过 —— 用 CPA 的客户端入口真打了一次业务请求」，
    而同一面板的 22 行全是「失败 / 未实测」。两处同时在说反话：
    标题把未实测说成已通过，表格把未实测说成失败。
    """

    def setUp(self):
        self.app = (Path(__file__).resolve().parents[1] / 'web/app.js').read_text(encoding='utf-8')

    def test_the_receipt_counts_passed_items_instead_of_everything_not_failed(self):
        # `verified.length - bad.length` 把跳过项算进通过数，因为 verify_failed
        # 按契约排除了 skipped。通过数必须数 ok 为真的项。
        self.assertNotIn('${d.verified.length - bad.length}', self.app,
                         'passed count must not be derived by subtracting only hard failures')
        self.assertRegex(self.app, r'filter\(\(?v\)? => v\.ok\)',
                         'the receipt must count items whose own result is ok')

    def test_skipped_verifications_render_as_not_measured(self):
        self.assertRegex(self.app, r'v\.skipped\s*\?', 'skipped items need their own pill')
        self.assertIn('未实测', self.app)

    def test_a_fully_skipped_verification_does_not_claim_a_real_request(self):
        self.assertRegex(self.app, r'allSkipped|skippedCount',
                         'the receipt must detect the case where every item was skipped')


class VerificationStatusContractTests(unittest.TestCase):
    """服务端必须把「跳过」与「失败」分开报告。"""

    def test_skipped_items_are_excluded_from_failures_and_counted_separately(self):
        server = (Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8')
        self.assertIn('not v.get("skipped")', server)
        self.assertIn('result["verify_passed"]', server)


if __name__ == '__main__':
    unittest.main()
