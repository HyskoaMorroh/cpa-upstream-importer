"""mihomo healthcheck 回归：严格 2xx、强制走代理、硬上限、降级与恢复。

2026-10-13 重写说明
------------------
本文件原名与原断言钉的是「只读、不发 PUT、不切换 DIRECT/AUTO」。那个前提
是错的：这份 healthcheck **两个项目共用**（cpa2sub2api 与本项目），而
cpa2sub2api 的 README 第一行就把「可切换的代理出口」写成核心用途 ——
用于绕开按 IP 拦截的上游站点。共享 compose 里只有一个 mihomo 容器、执行
同一份脚本，所以「只读」等于把对侧项目的核心功能删掉。

git 对照（实测）：HEAD 的 healthcheck.sh/.py 与 cpa2sub2api 现役版本
**逐字节相同**（201 行 / 169 行，带 switch_to + 状态文件），而工作树一度
被砍到 79 行 / 66 行、switch 命中 0。那个 commit 的标题本身写着
「healthcheck（两项目共用）」。

所以本文件改为钉**并集**契约：
  · 只读版引入的三项真改进要保留（严格 2xx + 禁重定向、强制走代理且清空
    NO_PROXY、子进程 30 秒硬上限）；
  · 基线的降级与恢复要能工作（出不去切 DIRECT、恢复切回 AUTO）；
  · 密钥不得泄漏到出网探测地址。

不发任何真实网络请求：假 API 与假代理都是本地回环 HTTP 服务。
"""
import importlib.util
import json
import subprocess
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / "mihomo-manager/mihomo/healthcheck.py"
SPEC = importlib.util.spec_from_file_location("mihomo_healthcheck", PATH)
healthcheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(healthcheck)


@contextmanager
def endpoint(status=204, delay=0, *, nodes=2):
    """假服务器。

    `nodes` 让 `/proxies/AUTO` 回一个**真实形状**的组对象 —— 脚本用
    `all` 的长度判断「这个组还有没有节点」。原版对所有 GET 一律回 204
    空体，于是 node_count 恒为 0，判定必然走降级分支：那是夹具不够真，
    不是被测代码的问题。
    """
    events = []

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            events.append(("GET", self.path, self.headers.get("Authorization")))
            time.sleep(delay)
            # 组查询要回组结构；其余（/version、出网探测）按参数化状态回。
            if status == 204 and self.path.startswith("/proxies/"):
                self._json({"name": self.path.rsplit("/", 1)[-1],
                            "type": "Selector",
                            "now": "AUTO",
                            "all": ["AUTO", "DIRECT"][:max(0, nodes)]})
                return
            self.send_response(status)
            self.send_header("Content-Length", "0")
            if status == 302:
                self.send_header("Location", "/redirected")
            self.end_headers()

        def do_PUT(self):
            events.append(("PUT", self.path, None))
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), events
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class HealthcheckContractTests(unittest.TestCase):
    def run_check(self, api, proxy, *, state=None):
        """跑一轮判定。`state` 模拟上一轮的降级状态文件。"""
        overrides = dict(API=api, PROXY=proxy,
                         PROBE_URL="http://must-use-proxy.invalid/probe",
                         SECRET="test-only")
        with patch.multiple(healthcheck, **overrides), \
                patch.dict("os.environ", {"NO_PROXY": "*", "no_proxy": "*"}):
            if state is not None:
                with patch.object(healthcheck, "read_state", lambda: state), \
                        patch.object(healthcheck, "write_state", lambda v: None):
                    return healthcheck.run_checks()
            with patch.object(healthcheck, "write_state", lambda v: None):
                return healthcheck.run_checks()

    # ---- 只读版引入的三项改进：必须保留 --------------------------------

    def test_success_forces_proxy_even_with_no_proxy_and_keeps_secret_local(self):
        """出网探测必须真的走代理，且密钥不能跟着发到探测地址。

        `NO_PROXY=*` 已设：代码若让环境变量插手，就会直连 .invalid 域名
        并失败 —— 或更糟，在真实环境里直连成功而把「代理可用」误判成真。
        """
        with endpoint() as (api, api_events), endpoint() as (proxy, proxy_events):
            self.assertEqual(self.run_check(api, proxy), 0)
            # API 侧：判活 + 读 AUTO 组，都带 Authorization
            self.assertEqual([(m, p) for m, p, _ in api_events],
                             [("GET", "/version"), ("GET", "/proxies/AUTO")])
            for _m, _p, auth in api_events:
                self.assertEqual("Bearer test-only", auth)
            # 代理侧：请求确实经过代理（绝对 URL 形态），且**不带**密钥
            self.assertEqual(
                proxy_events,
                [("GET", "http://must-use-proxy.invalid/probe", None)])

    def test_api_failure_is_unhealthy(self):
        """API 不响应说明进程坏了 —— 这一步失败不该去切 DIRECT。"""
        with endpoint(status=500) as (api, api_events), endpoint() as (proxy, _):
            self.assertEqual(self.run_check(api, proxy), 1)
            self.assertNotIn("PUT", [m for m, _p, _a in api_events])

    def test_proxy_redirect_is_not_treated_as_reachable(self):
        """重定向到门户页不算链路通（严格 2xx + 禁跟随）。

        代理探测失败后会降级，所以这里**预期**看到 PUT；要钉的是
        「30x 不算通」，不是「不许 PUT」。
        """
        with endpoint() as (api, api_events), endpoint(status=302) as (proxy, _):
            code = self.run_check(api, proxy)
            puts = [p for m, p, _a in api_events if m == "PUT"]
            self.assertEqual(["/proxies/" + healthcheck.GROUP], puts,
                             "探测到 30x 应判不通并降级一次")
            # 直连也走同一个假代理地址（.invalid），必然失败 → 不健康
            self.assertEqual(1, code)

    def test_outer_deadline_is_enforced_and_failure_reported(self):
        with patch.object(healthcheck.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("child", 30)) as run:
            self.assertEqual(healthcheck.main(), 1)
            self.assertEqual(1, run.call_count)

    def test_real_subprocess_deadline_kills_stalled_child(self):
        def stalled_child(*args, **kwargs):
            raise subprocess.TimeoutExpired("child", kwargs.get("timeout", 30))

        with patch.object(healthcheck, "TOTAL_TIMEOUT", 1), \
                patch.object(healthcheck.subprocess, "run",
                             side_effect=stalled_child):
            self.assertEqual(healthcheck.main(), 1)

    # ---- 基线的降级与恢复：不得再被删掉 --------------------------------

    def test_egress_failure_switches_to_direct(self):
        """代理出不去就把组切到 DIRECT —— cpa2sub2api 依赖这条。"""
        with endpoint() as (api, api_events), endpoint(status=500) as (proxy, _):
            self.run_check(api, proxy)
            puts = [(m, p) for m, p, _a in api_events if m == "PUT"]
            self.assertEqual([("PUT", "/proxies/" + healthcheck.GROUP)], puts)

    def test_recovery_switches_back_to_auto(self):
        """上一轮降级过、这一轮能出网 —— 必须切回 AUTO，不能停在 DIRECT。"""
        with endpoint() as (api, api_events), endpoint() as (proxy, _):
            self.assertEqual(self.run_check(api, proxy, state="DIRECT"), 0)
            puts = [(m, p) for m, p, _a in api_events if m == "PUT"]
            self.assertEqual([("PUT", "/proxies/" + healthcheck.GROUP)], puts,
                             "恢复时没有切回 AUTO")

    def test_no_switch_when_already_auto_and_healthy(self):
        """一切正常时不该有任何写操作 —— 避免每 60 秒白发一次 PUT。"""
        with endpoint() as (api, api_events), endpoint() as (proxy, _):
            self.assertEqual(self.run_check(api, proxy, state="AUTO"), 0)
            self.assertEqual([], [m for m, _p, _a in api_events if m == "PUT"])

    def test_switch_target_follows_group_env(self):
        """切换的组名读 MIHOMO_GROUP，不写死 PROXY。"""
        with endpoint() as (api, api_events), endpoint(status=500) as (proxy, _):
            with patch.object(healthcheck, "GROUP", "CUSTOM-GROUP"):
                self.run_check(api, proxy)
            puts = [p for m, p, _a in api_events if m == "PUT"]
            self.assertEqual(["/proxies/CUSTOM-GROUP"], puts)

    def test_empty_group_counts_as_no_egress(self):
        """组里一个节点都没有时，即便探测侥幸通过也要降级。"""
        with endpoint(nodes=0) as (api, api_events), endpoint() as (proxy, _):
            self.run_check(api, proxy)
            puts = [p for m, p, _a in api_events if m == "PUT"]
            self.assertEqual(["/proxies/" + healthcheck.GROUP], puts)

    # ---- 共用契约本身 --------------------------------------------------

    def test_switching_helpers_exist(self):
        """防止再有人把降级能力整块删掉。"""
        for name in ("switch", "read_state", "write_state",
                     "probe_via_proxy", "probe_direct", "run_checks", "check"):
            self.assertTrue(callable(getattr(healthcheck, name, None)),
                            f"{name} 不见了 —— 两项目共用的降级契约被破坏")

    def test_shell_and_python_keep_the_same_capabilities(self):
        """shell 版服务 mihomo 容器、python 版服务本镜像，功能必须对齐。"""
        shell = (PATH.parent / "healthcheck.sh").read_text(encoding="utf-8")
        for token in ("switch_to", "read_state", "write_state",
                      "probe_via_proxy", "probe_direct",
                      "MIHOMO_STATE_FILE", "MIHOMO_GROUP",
                      "no_proxy", "--bounded-child"):
            self.assertIn(token, shell, f"healthcheck.sh 缺 {token}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
