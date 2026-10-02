"""Run legacy protocol tests with an explicit synthetic-only policy double.

These tests exercise protocol parsing/retries rather than the policy ledger.
The new policy and passive-flow suites run WITHOUT this double and use the real
engine. No real hostname or non-loopback IP is authorized by this test helper.
"""
from pathlib import Path
import ipaddress
import runpy
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from cpa_probe import client, probe_policy, writeback
import cpa_probe


class SyntheticPolicy:
    @staticmethod
    def synthetic(url):
        try:
            host = (urlsplit(url).hostname or '').lower()
            if host == 'localhost' or host in ('example.com', 'example.net', 'example.org'):
                return True
            if host.endswith(('.example', '.invalid', '.test')):
                return True
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def check(self, url, headers=None):
        allowed = self.synthetic(url)
        return probe_policy.Decision(allowed, '' if allowed else 'test_host_denied',
                                     '' if allowed else '测试不允许访问真实上游')

    def reserve(self, url, headers=None):
        decision = self.check(url, headers)
        return probe_policy.Permit(decision.allowed, decision.code, decision.reason,
                                   'synthetic-test-reservation' if decision.allowed else '')

    def finish(self, permit, status, body='', error=''):
        pass

    def summary(self):
        return {'mode': 'restricted', 'authorized_sites': 0,
                'reason': '仅用于协议测试的合成端点替身'}


def main():
    path = Path(sys.argv[1]).resolve()
    if path.parent != Path(__file__).resolve().parent:
        raise SystemExit('Only a test file in this directory may be executed')
    policy = SyntheticPolicy()
    probe_policy.get_policy = lambda: policy
    # client may bind the accessor at module load; cover that explicit seam too.
    client.get_policy = lambda: policy
    original_verify = writeback.verify_upstream
    def verify_direct_fixture(*args, **kwargs):
        if args and policy.synthetic(args[0]):
            scope = kwargs.setdefault('scope', {})
            scope['direct_target'] = True
        return original_verify(*args, **kwargs)
    writeback.verify_upstream = verify_direct_fixture
    cpa_probe.verify_upstream = verify_direct_fixture
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path), *sys.argv[2:]]
    runpy.run_path(str(path), run_name='__main__')


if __name__ == '__main__':
    main()
