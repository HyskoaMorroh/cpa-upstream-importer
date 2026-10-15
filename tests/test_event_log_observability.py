"""事件流必须同步落进日志，且落盘内容不得带凭据或完整私有域名。

现场（本机实测 2026-10-13，真实 config.yaml + 10 把真实 Key）
--------------------------------------------------------
`IMPORTER_LOG_LEVEL=DEBUG` 与 `IMPORTER_LOG_TO_FILE=1` 全开，10 个候选、
421 次推理尝试跑完 1913.8 秒，**落盘日志只有 1 行**：

    [INFO] importer.server: 异步定档完成：1.2 秒 · 全量重探=False · 10 个候选

原因是 `Job.emit` 只往内存事件表 append，一个 logging 调用都没有。每站每段
的判定（目录读到几个模型、画像走到哪一档、被哪条策略挡住）只存在于浏览器的
事件流里 —— 浏览器一关、容器一换就没了。而部署方式是「VPS 只拉镜像」，
`pull_policy: always` 每次 `up -d` 都重建容器。于是要求第 10 条
（「支持详细的日志输出以便出错误的时候进行及时排查」）在最需要它的场景下
完全不成立：VPS 上 `docker compose logs upstream-importer` 捞不到任何线索。

本文件钉四条不变式：
  1. 每个 emit 都产出一条 DEBUG 日志（事件不再只存在于前端）；
  2. 日志里**没有**凭据明文，也没有完整私有域名（只留词干）；
  3. host 词干与前端 `tag()` 同口径，不同站仍能区分；
  4. 日志路径自身的异常绝不影响探测（emit 不得因日志抛出）。

不发任何网络请求、不碰账本：只构造 Job 并观察 logging 输出。
"""
from __future__ import annotations

import importlib.util
import io
import logging
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_job_class():
    """只取 `Job` 这一段来执行，不导入整个 server.py。

    为什么不 `import server`：模块顶层会解析 argv、建 BOOT_ID、准备
    HTTP handler，还会按环境变量配置 logging —— 那会污染本测试自己装的
    handler，也让失败原因变得难读。`Job` 只依赖 logging / time / threading，
    单独 exec 足够且更稳。
    """
    src = io.open(ROOT / "server.py", encoding="utf-8").read()
    match = re.search(r"\nclass Job:.*?\n    def mark_unit_start", src, re.S)
    if not match:
        raise AssertionError("server.py 里找不到 Job 类 —— 测试需要跟着改")
    body = match.group(0).rsplit("\n    def mark_unit_start", 1)[0]
    namespace: dict = {}
    exec("import logging, time, threading\n"
         'logger = logging.getLogger("importer.server")\n' + body, namespace)
    return namespace["Job"]


Job = _load_job_class()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):          # noqa: A003 - logging API
        self.records.append(record)

    @property
    def lines(self) -> list[str]:
        return [r.getMessage() for r in self.records]


class EventLoggingTests(unittest.TestCase):
    def setUp(self):
        self.capture = _Capture()
        self.logger = logging.getLogger("importer.server")
        self.prior_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.capture)
        self.addCleanup(self.logger.removeHandler, self.capture)
        self.addCleanup(self.logger.setLevel, self.prior_level)
        self.job = Job("abcdef0123456789", [], {})

    def test_every_emit_produces_a_debug_line(self):
        """原来一整轮探测只落 1 行 —— 这条防它退回去。"""
        self.job.emit("attempt", {"host": "relay.example.com",
                                  "section": "codex-api-key",
                                  "model": "gpt-6-astra", "status": "200",
                                  "category": "可用", "elapsed_ms": 1234})
        self.job.emit("catalog", {"host": "relay.example.com",
                                  "section": "claude-api-key", "count": 3})
        self.assertEqual(2, len(self.capture.records))
        for record in self.capture.records:
            self.assertEqual(logging.DEBUG, record.levelno)

    def test_log_line_carries_the_diagnostic_fields(self):
        """日志要能回答「哪个站、哪一段、哪个模型、什么结果」。"""
        self.job.emit("attempt", {"host": "relay.example.com",
                                  "section": "codex-api-key",
                                  "model": "gpt-6-astra", "status": "401",
                                  "category": "客户端", "elapsed_ms": 55})
        line = self.capture.lines[0]
        for needle in ("attempt", "relay", "codex-api-key", "gpt-6-astra",
                       "status=401", "category=客户端", "elapsed_ms=55"):
            self.assertIn(needle, line, line)

    def test_job_id_prefix_allows_correlating_lines(self):
        """多任务并发时日志是交织的，没有任务前缀就分不开。"""
        self.job.emit("info", {"phase": "probe"})
        self.assertIn("[abcdef01]", self.capture.lines[0])

    def test_credentials_never_reach_the_log(self):
        """凭据明文绝不进日志 —— 日志目录是 bind-mount 出去的。

        cpa_probe 侧用 `row.masked()`，这里再挡一道：api_key 这类字段
        不在白名单里，不会被原样记出来。
        """
        self.job.emit("candidate-start", {
            "host": "relay.example.com",
            "key": "sk-REAL1234567890abcdef",
            "api_key": "sk-ANOTHERSECRET0987654321",
            "authorization": "Bearer sk-THIRDSECRET",
        })
        line = self.capture.lines[0]
        for secret in ("sk-REAL1234567890abcdef", "sk-ANOTHERSECRET0987654321",
                       "sk-THIRDSECRET", "Bearer"):
            self.assertNotIn(secret, line, line)

    def test_full_private_hostname_never_reaches_the_log(self):
        """只留词干。完整私有域名落盘等于把上游清单交出去。"""
        self.job.emit("attempt", {"host": "api.my-private-relay.example.net",
                                  "section": "codex-api-key", "status": "200"})
        line = self.capture.lines[0]
        self.assertNotIn("api.my-private-relay.example.net", line)
        self.assertNotIn(".example.net", line)
        self.assertIn("my-private-r", line, "词干丢了就没法区分站点")

    def test_host_stem_matches_the_frontend_convention(self):
        """与 web/app.js 的 `tag()` 同口径，否则日志与界面对不上。

        词干规则：取倒数第三段；若它是 `api` / `www` 这类无区分度的前缀，
        则退一位取倒数第二段。用例覆盖三段、四段（多级后缀）与两段三种形态。
        """
        cases = {
            # 三段 + 无区分度前缀 -> 退一位
            "api.foo.example": "foo",
            "www.bar.example": "bar",
            # 四段（多级后缀）-> 倒数第三段就是站名
            "sub.foo.co.uk": "foo",
            # 两段 -> 取首段
            "relay-one.example": "relay-one",
            "": "-",
        }
        for host, want in cases.items():
            with self.subTest(host=host):
                self.assertEqual(want, Job._log_host(host))

    def test_distinct_sites_stay_distinguishable(self):
        """脱敏不能脱到「两个站看起来一样」—— 那就失去排障价值。"""
        a = Job._log_host("api.alpha-relay.com")
        b = Job._log_host("api.beta-relay.com")
        self.assertNotEqual(a, b)

    def test_unknown_fields_are_dropped_not_guessed(self):
        """白名单而非黑名单：将来加字段时漏一次判断的代价是泄密。"""
        self.job.emit("attempt", {"host": "relay.example.com",
                                  "some_future_field": "RAW-SENSITIVE-VALUE",
                                  "status": "200"})
        line = self.capture.lines[0]
        self.assertNotIn("RAW-SENSITIVE-VALUE", line)
        self.assertIn("status=200", line)

    def test_long_values_are_truncated(self):
        """单条日志不该被一个巨大的 reason 撑爆。"""
        self.job.emit("attempt", {"host": "relay.example.com",
                                  "reason": "x" * 5000})
        line = self.capture.lines[0]
        self.assertLess(len(line), 600, "长字段没有截断")
        self.assertIn("...", line)

    def test_logging_failure_cannot_break_emit(self):
        """日志出问题也不能影响探测 —— 事件表仍要写进去。"""
        class Boom(logging.Handler):
            def emit(self, record):      # noqa: A003
                raise RuntimeError("handler exploded")

        boom = Boom(level=logging.DEBUG)
        self.logger.addHandler(boom)
        self.addCleanup(self.logger.removeHandler, boom)
        # 不抛，且事件仍然记下来了
        self.job.emit("attempt", {"host": "relay.example.com", "status": "200"})
        self.assertEqual(1, len(self.job.events))
        self.assertEqual("attempt", self.job.events[0]["kind"])

    def test_event_table_still_works_as_before(self):
        """加日志不得改动原有的事件表语义（要求第 8 条：只能正向）。"""
        self.job.emit("info", {"phase": "setup", "msg": "开始"})
        self.job.emit("attempt", {"host": "relay.example.com", "status": "200"})
        self.assertEqual("setup", self.job.phase)
        self.assertEqual("开始", self.job.phase_message)
        self.assertEqual(1, self.job.calls, "attempt 计数没加")
        self.assertEqual([1, 2], [e["seq"] for e in self.job.events])


if __name__ == "__main__":
    unittest.main(verbosity=2)
