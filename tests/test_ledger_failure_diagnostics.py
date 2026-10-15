"""账本不可用时必须留下诊断 —— 修改要求第 10 条。

现场（2026-10-12 本机实跑，真实 config.yaml + 真实 accounts.txt）
------------------------------------------------------------
`_attempt` 里那个 `except (sqlite3.Error, OSError, ValueError, TypeError,
OverflowError): return _decision("ledger_unavailable")` 原来把异常**整个吞掉**，
一行日志都不记。

这是全系统最要紧的 fail-closed 判定：账本打不开时每一次上游请求都被拒，
界面上每个段显示「未实测」、定档 0 个方案、所有参数空白 —— 而日志里**没有
任何**线索说明为什么。现场只能看到「什么信息都没有」，无从下手。

这正是 `tests/test_deploy_runtime_contracts.py` 记录的 2026-10-04 生产事故
形态：镜像把 /backups chown 给 APP_UID 并留 0755，而 compose 以 uid 0 运行且
`cap_drop: [ALL]` —— root 没有 CAP_DAC_OVERRIDE，建不了
probe-policy.sqlite3，于是八个账号四段全是「未实测」、零请求。

那次事故靠人读源码才定位到。这条用例钉住「下次它自己会说」。

范围说明
--------
本文件**只**管诊断可观测性，不碰任何判定语义。目录请求与判死作用域的契约
由 `tests/test_catalog_restriction_scope.py` 负责，那份契约区分得很细
（凭据级受限 / 目录门禁 / 站级明确拒绝各有不同作用域），不要在这里重复或
与之冲突。
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cpa_probe import probe_policy as pp


class LedgerFailureIsLoggedTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-diag-")
        self._saved = {k: os.environ.get(k) for k in
                       ("IMPORTER_PROBE_MODE", "IMPORTER_PROBE_LEDGER",
                        "IMPORTER_PROBE_POLICY_FILE", "IMPORTER_BACKUP_DIR")}
        os.environ["IMPORTER_PROBE_MODE"] = "managed"
        os.environ["IMPORTER_PROBE_LEDGER"] = os.path.join(
            self.dir, "probe-policy.sqlite3")
        os.environ.pop("IMPORTER_PROBE_POLICY_FILE", None)
        os.environ.pop("IMPORTER_BACKUP_DIR", None)
        pp._cached_policy = None
        pp._cached_paths = None

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        pp._cached_policy = None
        pp._cached_paths = None

    def test_unopenable_ledger_logs_an_actionable_error(self):
        policy = pp.get_policy()
        # 把账本路径指到一个**普通文件**底下：任何 sqlite 打开尝试都会失败，
        # 且不依赖文件权限（Windows 上 chmod 不可靠）。
        blocker = Path(self.dir) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        policy._ledger_path = str(blocker / "probe-policy.sqlite3")

        with self.assertLogs("cpa_probe.probe_policy", level="ERROR") as caught:
            decision = policy.check("https://unit.example/v1/messages")

        self.assertEqual(decision.code, "ledger_unavailable",
                         "账本不可用时必须 fail-closed 拒绝请求")
        joined = "\n".join(caught.output)
        self.assertIn("持久账本不可用", joined,
                      "必须说清是账本问题，而不是让现场去猜「未实测」的原因")
        self.assertIn("IMPORTER_PROBE_LEDGER", joined,
                      "必须指出去哪儿找账本路径，否则排查无从下手")
        # 异常类型要带上：权限、磁盘满、库被锁、路径不存在各有不同处置。
        self.assertRegex(joined, r"(OperationalError|OSError|DatabaseError|Error)")

    def test_diagnostic_never_leaks_credentials(self):
        policy = pp.get_policy()
        blocker = Path(self.dir) / "blocked"
        blocker.write_text("x", encoding="utf-8")
        policy._ledger_path = str(blocker / "probe-policy.sqlite3")
        with self.assertLogs("cpa_probe.probe_policy", level="ERROR") as caught:
            policy.check("https://unit.example/v1/messages",
                         {"Authorization": "Bearer synthetic-secret-value"})
        joined = "\n".join(caught.output)
        self.assertNotIn("synthetic-secret-value", joined)
        self.assertNotIn("Bearer ", joined)


if __name__ == "__main__":
    unittest.main()
