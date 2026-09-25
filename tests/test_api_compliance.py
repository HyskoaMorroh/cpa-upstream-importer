"""Task 6: synthetic-only API/CLI and bulk regression tests."""
import concurrent.futures
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 与其余套件一致：自己插 sys.path，不依赖调用方设 PYTHONPATH。
# 漏了这两行就只能 `PYTHONPATH=. python tests/test_api_compliance.py` 才跑得起来
# （`import server` 直接 ModuleNotFoundError），而 tests/run.py 是按路径起
# 子进程的 —— 那等于这个套件永远不进主入口。2026-09-12 补。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
import server
import cli
from cpa_probe import bulk


RAW = """codex-api-key:
  - api-key: fixture-secret-one
    base-url: https://fixture.invalid/v1
    priority: 7
    excluded-models:
      - old-model
    unknown-field: keep
  - api-key: fixture-secret-two
    base-url: https://fixture.invalid/v1
  - api-key: fixture-secret-three
    base-url: https://fixture.invalid/v1
"""


def handler(raw=RAW):
    h = object.__new__(server.Handler)
    h.headers = {}
    h.client_address = ("127.0.0.1", 1234)
    h._load_cfg = lambda: (raw, yaml.safe_load(raw))
    h._json = lambda code, data: setattr(h, "response", (code, data))
    h._cpa_password_for = lambda body: "fixture-management"
    h._cpa_client_key = lambda: ""
    return h


class ComplianceTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect",
                                    side_effect=AssertionError("External network forbidden"))
        self.connect = self.network.start()
        self.addCleanup(self.network.stop)
        self.semantics = patch.object(
            bulk, "disable_semantics", return_value=("*", "disabled", "fixture"))
        self.semantics.start()
        self.addCleanup(self.semantics.stop)
        self.output_context = patch.object(cli, "_OUTPUT_CONTEXT", {})
        self.output_context.start()
        self.addCleanup(self.output_context.stop)

    def tearDown(self):
        self.connect.assert_not_called()

    def test_duplicate_delete_only_removes_selected_key(self):
        op = {"section": "codex-api-key", "index": 0, "action": "delete",
              "expect": "https://fixture.invalid/v1"}
        out, _, problems = bulk.apply_bulk(RAW, [op, dict(op)])
        self.assertFalse(problems)
        self.assertEqual(len(yaml.safe_load(out)["codex-api-key"]), 2)

    def test_conflicting_operations_leave_original_untouched(self):
        ops = [{"section": "codex-api-key", "index": 0, "action": a}
               for a in ("enable", "disable")]
        out, notes, problems = bulk.apply_bulk(RAW, ops)
        self.assertEqual(out, RAW)
        self.assertFalse(notes)
        self.assertTrue(problems)

    def test_block_exclusions_roundtrip_preserves_unknown(self):
        op = {"section": "codex-api-key", "index": 0, "action": "disable"}
        out, notes, problems = bulk.apply_bulk(RAW, [op])
        self.assertFalse(problems)
        self.assertTrue(notes)
        entry = yaml.safe_load(out)["codex-api-key"][0]
        self.assertEqual(entry["excluded-models"], ["old-model", "*"])
        self.assertEqual(entry["unknown-field"], "keep")
        op["action"] = "enable"
        restored, _, problems = bulk.apply_bulk(out, [op])
        self.assertFalse(problems)
        self.assertEqual(yaml.safe_load(restored), yaml.safe_load(RAW))

    def test_priority_includes_implicit_zero(self):
        ops = bulk.unify_priority_ops(yaml.safe_load(RAW),
                                      "codex-api-key", "fixture.invalid")
        self.assertEqual({o["index"] for o in ops}, {1, 2})

    def test_public_ipv6_is_not_a_service_name(self):
        self.assertTrue(server._push_target_ok("http://[2606:4700::1111]:8317", ""))
        self.assertFalse(server._push_target_ok("http://[::1]:8317", ""))
        self.assertFalse(server._push_target_ok("http://service:8317", ""))
        self.assertFalse(server._push_target_ok(
            "https://[2606:4700::1111]", "https://[2606:4700::1111]"))

    def test_body_rejects_nonobject_and_truthy_booleans(self):
        for value in ([], None, {"confirm": "false"},
                      {"opts": {"probe_context": "false"}}):
            h = handler()
            data = json.dumps(value).encode()
            h.headers = {"Content-Length": str(len(data))}
            h.rfile = io.BytesIO(data)
            with self.assertRaises(ValueError):
                h._body()

    def test_invalid_model_override_refuses(self):
        with self.assertRaises(ValueError):
            server._clean_override_models("claude-api-key", ["gpt-5"])

    def test_event_cursor_survives_retention(self):
        job = server.Job("fixture-job", [], {})
        job.MAX_EVENTS = 4
        job.KEEP_HEAD = 1
        for i in range(8):
            job.emit("info", {"msg": str(i)})
        snap = job.snapshot(0)
        cursor = snap["event_cursor"]
        job.emit("info", {"msg": "latest"})
        nxt = job.snapshot(cursor)
        self.assertGreater(nxt["event_cursor"], cursor)
        self.assertEqual(nxt["events"][-1]["msg"], "latest")
        self.assertTrue(snap["history_lost"])

    def test_job_admission_includes_pending(self):
        store = server.Store()
        store.MAX_JOBS = 1
        store.add_job(server.Job("one", [], {}))
        with self.assertRaises(server.CapacityError):
            store.add_job(server.Job("two", [], {}))
        self.assertEqual(len(store.jobs), 1)

    def test_trusted_proxy_chain_and_spoof(self):
        h = handler()
        h.client_address = ("172.20.0.2", 1)
        h.headers = {"X-Forwarded-For": "192.0.2.99, 198.51.100.7, 172.20.0.3"}
        with patch.object(server.Handler, "trusted_proxy_peers",
                          ("172.20.0.2/32", "172.20.0.3/32"), create=True):
            self.assertEqual(h._client_ip(), "198.51.100.7")
        with patch.object(server.Handler, "trusted_proxy_peers", (), create=True):
            self.assertEqual(h._client_ip(), "172.20.0.2")

    def test_requestline_query_is_not_logged(self):
        h = handler()
        h.path = "/api/context?token=fixture-query-secret"
        h.command = "GET"
        h.requestline = "GET " + h.path + " HTTP/1.1"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            h.log_message('"%s" %s %s', h.requestline, "200", "-")
        self.assertNotIn("fixture-query-secret", buf.getvalue())

    def test_invalid_cli_row_never_prints_raw(self):
        row = SimpleNamespace(line_no=1, error="invalid", raw="fixture-row-secret")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_parse(SimpleNamespace(valid=[], invalid=[row]))
        self.assertNotIn("fixture-row-secret", buf.getvalue())

    def test_stale_bulk_revision_refused(self):
        h = handler()
        h._api_bulk_preview({"revision": "stale", "ops": [
            {"section": "codex-api-key", "index": 0, "action": "disable"}]})
        self.assertEqual(h.response[0], 409)

    # ── 批量设档的站间撞值消解（2026-09-13）───────────────────────────
    #
    # 用户第 3⑶ / 第 7 条：同类型不同域名的优先级一定要不同。
    # 「批量设为 N」是跨组动作，一次能命中几十个组 —— 全落同一个值就会被
    # CPA 的 selector 并进**一个桶**按 weight 轮询，站间次序被推平。
    #
    # 这条只在服务端测：单站直改与批量设档是两条路，两条都必须过同一道消解。
    # 前端只负责把用户填的值发过来，不参与错开。
    _COLLIDE = """codex-api-key:
  - api-key: fixture-a
    base-url: https://a.invalid/v1
    priority: 900
  - api-key: fixture-b
    base-url: https://b.invalid/v1
    priority: 800
  - api-key: fixture-c
    base-url: https://c.invalid/v1
    priority: 700
"""

    def test_bulk_setpri_offsets_colliding_hosts(self):
        h = handler(self._COLLIDE)
        raw = self._COLLIDE
        cfg = yaml.safe_load(raw)
        ops = [{"section": "codex-api-key", "index": i, "action": "priority",
                "value": 500,
                "fingerprint": bulk.entry_fingerprint(cfg["codex-api-key"][i])}
               for i in range(3)]
        h._api_bulk_preview({"revision": bulk.config_revision(raw), "ops": ops})
        code, body = h.response
        self.assertEqual(code, 200, body)
        # 三组原本都填 500，必须被错开成互不相同的值
        self.assertIn("collision_notes", body)
        self.assertEqual(len(body["collision_notes"]), 2,
                         "三组撞值应当只有两组被让位")
        for note in body["collision_notes"]:
            self.assertIn("撞档", note)

    def test_bulk_setpri_avoids_existing_tiers(self):
        """错开时不许撞上本段未被改动的在用站档位。

        撞上等于与那个站在同层按 weight 轮询 —— 而 priority 的唯一作用是
        区分先后。夹具 900/800/700 是 a/b/c 的现有档位，批量设 900 时
        三组要拿到三个互不相同的值，且都不等于 800 或 700。

        直接断言返回值而不是解析 diff：三组里字典序最小者（a）如愿拿 900，
        那一行**与原文相同**、不进 diff，用正则从 diff 里捞只能捞到两条。
        """
        cfg = yaml.safe_load(self._COLLIDE)
        ops = [{"section": "codex-api-key", "index": i, "action": "priority",
                "value": 900, "fingerprint": ""} for i in range(3)]
        out, notes = server.Handler._resolve_op_collisions(cfg, ops)
        got = sorted(o["value"] for o in out)
        self.assertEqual(len(set(got)), 3, f"三站没拿到不同档位：{got}")
        self.assertNotIn(0, got, "priority 0 语义未定义，不许写出去")
        for reserved in (800, 700):
            self.assertNotIn(reserved, got,
                             f"撞上了未被改动的在用站档位 {reserved}")
        self.assertEqual(len(notes), 2, notes)

    def test_resolve_op_collisions_leaves_non_priority_ops_alone(self):
        """只改启停的 ops 不该被消解动到。"""
        h = handler(self._COLLIDE)
        ops = [{"section": "codex-api-key", "index": 0, "action": "disable"},
               {"section": "codex-api-key", "index": 1, "action": "enable"}]
        out, notes = server.Handler._resolve_op_collisions(
            yaml.safe_load(self._COLLIDE), list(ops))
        self.assertEqual(notes, [])
        self.assertEqual(out, ops)

    def test_apply_requires_exact_true(self):
        store = server.Store()
        store.add_plan("p", {"preview": RAW, "base_raw": RAW, "diffs": []})
        h = handler()
        with patch.object(server, "STORE", store):
            h._api_apply({"plan_id": "p", "confirm": "false"})
        self.assertEqual(h.response[0], 400)

    def test_apply_concurrent_confirmation_returns_same_task(self):
        raw = "codex-api-key: []\n"
        store = server.Store()
        store.add_plan("p", {"preview": raw, "base_raw": raw,
                             "plans": [], "diffs": []})
        handlers = [handler(raw), handler(raw)]
        with patch.object(server, "STORE", store), \
                patch.object(server, "_start_apply_task", create=True) as start:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda h: h._api_apply(
                    {"plan_id": "p", "confirm": True}), handlers))
        self.assertEqual(start.call_count, 1)
        self.assertEqual(handlers[0].response[1]["task_id"],
                         handlers[1].response[1]["task_id"])
        self.assertNotIn("written", handlers[0].response[1])

    def test_cross_protocol_priority_is_rejected(self):
        raw = """codex-api-key:
  - api-key: fixture-one
    base-url: https://fixture.invalid/v1
    priority: 7
claude-api-key:
  - api-key: fixture-two
    base-url: https://fixture.invalid
"""
        self.assertFalse(server._validate_final(raw)[0])
        ops = bulk.unify_priority_ops(yaml.safe_load(raw), "codex-api-key",
                                      "fixture.invalid", 7)
        self.assertEqual([o["section"] for o in ops], ["claude-api-key"])
        out, _, problems = bulk.apply_bulk(raw, ops)
        self.assertFalse(problems)
        self.assertTrue(server._validate_final(out)[0])

    def test_public_output_keeps_types_and_redacts_credentials(self):
        payload = {
            "headers": {"Authorization": "Bearer fixture-header-secret",
                        "X-Custom": "fixture-custom-secret"},
            "proxy_url": "http://fixture-user:fixture-pass@fixture.invalid?token=fixture-url-secret",
            "disable_cooling": False, "prompt_cache_key": True,
            "model_provenance": {"gpt-5": "probed"}, "task_id": "fixture-task",
        }
        result = server._public(payload)
        public = json.dumps(result)
        for secret in ("fixture-header-secret", "fixture-custom-secret",
                       "fixture-user", "fixture-pass", "fixture-url-secret"):
            self.assertNotIn(secret, public)
        # 主机名**保留**（2026-09-12 改）。
        #
        # 这一行原来要求把域名也抹掉。那是把两层职责混在一起了：
        #   · 本地响应这一层 —— 界面就跑在操作员自己机器上，全部工作靠站名
        #     来做（/api/parse 回的 base-url、方案里的 base_url、diff 里的
        #     config.yaml 行）。域名改写之后界面上认不出是哪个站，也没法复核
        #     要写什么进配置。server.py 打印的那句说明与 tests/test_server.py
        #     钉的契约都是「主机与端口要留着 —— 排障最需要看那部分」。
        #   · **提交到 GitHub** 这一层 —— docx 第 9 条要求的私有域名脱敏在
        #     `tools/scrub.py`（DOMAIN_MAP 把真实域名换成占位符）加 .gitignore，
        #     下面 test_private_domains_are_scrubbed_before_publish 钉住它。
        self.assertIn("fixture.invalid", public)
        self.assertIs(result["disable_cooling"], False)
        self.assertIs(result["prompt_cache_key"], True)
        self.assertEqual(result["task_id"], "fixture-task")
        self.assertEqual(payload["headers"]["X-Custom"], "fixture-custom-secret")

    def test_runtime_revision_requires_authenticated_management(self):
        response = SimpleNamespace(headers={"X-CPA-COMMIT": "fixture-commit"})
        cm = contextlib.nullcontext(response)
        with patch.object(server.cp.client, "_opener") as opener, \
                patch.dict(server._CPA_COMMIT_CACHE, {"at": 0, "commit": ""}, clear=True):
            request = opener.return_value.open
            request.return_value = cm
            self.assertEqual(server._cpa_runtime_commit(
                "http://127.0.0.1:8317", "fixture-management"), "fixture-commit")
            req = request.call_args.args[0]
            self.assertEqual(urllib_path(req.full_url), "/v0/management/config.yaml")
            self.assertEqual(req.get_header("Authorization"), "Bearer fixture-management")
        with patch.object(server.cp.client, "_opener") as request:
            self.assertEqual(server._cpa_runtime_commit("http://127.0.0.1:8317"), "")
            request.assert_not_called()

    def test_bulk_tail_reaches_reload_and_reports_failure(self):
        raw = "codex-api-key: []\n"
        new = raw + "request-retry: 2\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text(raw, encoding="utf-8")
            inode = path.stat().st_ino
            task = server.ApplyTask("fixture-task", {"local_written": False})
            entry = {"text": new, "base_raw": raw, "notes": ["fixture"]}
            with patch.object(server, "STORE", server.Store()), \
                    patch.object(server, "reload_cpa", return_value=(False, "fixture reload failed")) as reload:
                server._commit_apply(task, entry, {}, str(path),
                                     "http://127.0.0.1:8317", "fixture-management", "", folder)
            self.assertEqual(path.read_text(encoding="utf-8"), new)
            self.assertEqual(path.stat().st_ino, inode)
            self.assertEqual(reload.call_args.args[2], new)
            self.assertIs(task.result["local_written"], True)
            self.assertEqual(task.state, "error")
            self.assertEqual(task.result["error_code"], "reload_failed")

    def test_refused_push_target_still_writes_config(self):
        """推送目标被拒 ≠ 不许写盘（2026-09-25 现场根因回归）。

        现场：用户在 ④ 面板把「CPA 地址」填成公网域名/缺 scheme 的地址，
        `_commit_apply` 在写盘**之前**就 `raise ValueError("管理目标被拒，
        未写盘")`，整次写回作废；而前端拿着这个 error 仍旧渲染「✓ 已写回」
        + 一排空的 written/backup/diffs。用户看到的是「写回成功但全是空白」，
        真相是一个字节都没写。

        白名单要防的是「整份配置被 PUT 给错误目标」—— 本地写盘不出网、
        不外发凭据，不该被这道闸连坐。所以本例断言三件事：
          · config.yaml **真的被改了**
          · reload_cpa **一次都没发**（凭据没出去）
          · 失败原因带得回来，且带上被拒地址
        """
        raw = "codex-api-key: []\n"
        new = raw + "request-retry: 2\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text(raw, encoding="utf-8")
            task = server.ApplyTask("fixture-task", {"local_written": False})
            entry = {"text": new, "base_raw": raw, "notes": ["fixture"]}
            body = {"push": {"base": "https://evil.example.com"}}
            with patch.object(server, "STORE", server.Store()), \
                    patch.object(server, "reload_cpa") as reload:
                server._commit_apply(task, entry, body, str(path),
                                     "http://cli-proxy-api:8317",
                                     "fixture-management", "", folder)
            self.assertEqual(path.read_text(encoding="utf-8"), new)
            self.assertIs(task.result["local_written"], True)
            self.assertTrue(task.result["written"])
            self.assertTrue(task.result["backup"])
            reload.assert_not_called()
            self.assertIs(task.result["reload_ok"], False)
            self.assertIn("evil.example.com", task.result["reload_msg"])
            self.assertIn("未触发 CPA 重载", task.result["reload_msg"])

    def test_scheme_less_push_base_reaches_reload(self):
        """`cli-proxy-api:8317` 少个 http:// 不能让整条重载链断掉。

        旧代码对缺 scheme 的地址回「配置推送地址格式无效」，配合上面那条
        「被拒就不写盘」，等于少打七个字符就让写回全废。补全 scheme 后，
        它应当与 `http://cli-proxy-api:8317` 完全等价 —— reload 真的发出去。
        """
        raw = "codex-api-key: []\n"
        new = raw + "request-retry: 3\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text(raw, encoding="utf-8")
            task = server.ApplyTask("fixture-task", {"local_written": False})
            entry = {"text": new, "base_raw": raw, "notes": ["fixture"]}
            body = {"push": {"base": "cli-proxy-api:8317"}}
            with patch.object(server, "STORE", server.Store()), \
                    patch.object(server, "reload_cpa",
                                 return_value=(False, "fixture reload failed")) as reload, \
                    patch.object(server.time, "sleep"):
                server._commit_apply(task, entry, body, str(path),
                                     "http://cli-proxy-api:8317",
                                     "fixture-management", "", folder)
            reload.assert_called_once()
            self.assertEqual(reload.call_args.args[0], "http://cli-proxy-api:8317")
            self.assertEqual(path.read_text(encoding="utf-8"), new)

    def test_stale_queued_work_never_writes_or_pushes(self):
        store = server.Store()
        first = {"preview": "request-retry: 1\n", "base_raw": "{}\n"}
        second = {"preview": "request-retry: 2\n", "base_raw": "{}\n"}
        old, _ = store.claim_apply(first)
        newer, _ = store.claim_apply(second)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text("{}\n", encoding="utf-8")
            with patch.object(server, "STORE", store), \
                    patch.object(server, "reload_cpa", return_value=(True, "fixture")) as reload, \
                    patch.object(server.time, "sleep"):
                server._commit_apply(newer, second, {}, str(path),
                                     "http://127.0.0.1:8317", "fixture-management", "", folder)
                server._commit_apply(old, first, {}, str(path),
                                     "http://127.0.0.1:8317", "fixture-management", "", folder)
            self.assertEqual(path.read_text(encoding="utf-8"), second["preview"])
            self.assertEqual(reload.call_count, 1)
            self.assertEqual(old.result["error_code"], "stale_queued_work")
            self.assertEqual(newer.state, "done")

    def test_verified_scope_is_gateway_only_without_unique_route(self):
        section = SimpleNamespace(writable=True, models=["gpt-5"])
        plans = [SimpleNamespace(host="fixture.invalid", sections={"codex-api-key": section})]
        task = server.ApplyTask("fixture-task", {})
        with patch.object(server, "reload_cpa", return_value=(True, "fixture")), \
                patch.object(server, "verify_upstream", return_value=(True, "fixture")), \
                patch.object(server.time, "sleep"):
            server._run_apply_tail(task, {"preview": "{}", "plans": plans}, {},
                                   "", "http://127.0.0.1:8317",
                                   "fixture-management", "fixture-client")
        verdict = task.result["verified"][0]
        self.assertEqual(verdict["verification_scope"], "gateway_only")
        self.assertIs(verdict["target_verified"], False)

    def test_cli_rechecks_snapshot_after_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = Path(folder) / "fixture.yaml"
            accounts = Path(folder) / "fixture.txt"
            cfg.write_text("request-retry: 1\n", encoding="utf-8")
            accounts.write_text("https://fixture.invalid,fixture-cli-key\n", encoding="utf-8")
            row = server.cp.parse_lines(accounts.read_text()).valid[0]
            def probe(_row):
                cfg.write_text("request-retry: 99\n", encoding="utf-8")
                return SimpleNamespace(row=row)
            prober = SimpleNamespace(probe=probe, workers=1)
            plan = SimpleNamespace(sections={})
            diff = SimpleNamespace(lines=["request-retry: 2"], section="fixture",
                                   host="fixture.invalid", insert_at=0)
            argv = ["cli", "--input", str(accounts), "--config", str(cfg),
                    "--no-proxy", "--no-context", "--write", "--no-reload"]
            with patch.object(cli.sys, "argv", argv), \
                    patch.object(cli, "Prober", return_value=prober), \
                    patch.object(cli.cp, "build_band", return_value={}), \
                    patch.object(cli.cp, "build_plan", return_value=plan), \
                    patch.object(cli.cp, "mark_new_sections", return_value=0), \
                    patch.object(cli.cp, "assign_priorities", return_value=[]), \
                    patch.object(cli, "_print_result"), \
                    patch.object(cli, "build_diffs", return_value=[diff]), \
                    patch.object(cli, "apply_diffs", return_value="request-retry: 2\n"), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(server.WritebackError):
                    cli.main()
            self.assertEqual(cfg.read_text(), "request-retry: 99\n")

    def test_probe_entries_pass_snapshot_and_keep_paths(self):
        raw = """claude-header-defaults: {user-agent: fixture-agent}
codex-api-key:
  - api-key: fixture-channel-key
    base-url: https://fixture.invalid/ChannelA
  - api-key: fixture-channel-key
    base-url: https://fixture.invalid/ChannelB
"""
        captured = []
        class FakeBatch:
            errors = []
            _stats = {"success": 2, "failure": 0}
            def __init__(self, prober, **kw):
                pass
            def probe_batch(self, rows, progress_callback):
                captured.extend(rows)
                for i, row in enumerate(rows, 1):
                    progress_callback(i, len(rows), row.host, self._stats)
                return {(r.bare, r.api_key): SimpleNamespace(row=r) for r in rows}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text(raw, encoding="utf-8")
            with patch.object(server, "Prober") as prober, \
                    patch.object(server, "BatchProber", FakeBatch), \
                    patch.object(server, "_resolve_proxy", return_value=None):
                job = server.Job("fixture-full", [], {})
                server.run_job_full_redetect(job, str(path))
                self.assertEqual(prober.call_args.kwargs["cfg_snapshot"], yaml.safe_load(raw))
                self.assertEqual({r.bare for r in captured},
                                 {"https://fixture.invalid/ChannelA",
                                  "https://fixture.invalid/ChannelB"})
                self.assertEqual(job.snapshot()["total_rows"], 2)
                self.assertEqual(job.snapshot()["unit_done"], 2)
                self.assertEqual(job.state, "done")
                new_job = server.Job("fixture-new", [captured[0]], {"candidate_workers": 200})
                prober.return_value.probe.return_value = SimpleNamespace(row=captured[0])
                server.run_job(new_job, str(path))
                self.assertEqual(prober.call_args.kwargs["cfg_snapshot"], yaml.safe_load(raw))
                self.assertEqual(server._clamp(new_job.opts, "candidate_workers", 16), 32)

    def test_login_management_credential_reused_without_body_cred(self):
        h = handler()
        del h._cpa_password_for
        h._validated_management = True
        h._validated_credential = "fixture-management"
        self.assertEqual(h._cpa_password_for({}), "fixture-management")

    def test_bulk_revision_binds_indices_when_keys_reorder(self):
        cfg = yaml.safe_load(RAW)
        cfg["codex-api-key"].reverse()
        reordered = yaml.safe_dump(cfg)
        h = handler(reordered)
        h._api_bulk_preview({"revision": bulk.config_revision(RAW), "ops": [
            {"section": "codex-api-key", "index": 0, "action": "delete",
             "fingerprint": bulk.entry_fingerprint(yaml.safe_load(RAW)["codex-api-key"][0])}]})
        self.assertEqual(h.response[0], 409)
        self.assertEqual(h.response[1]["error_code"], "stale_selection")

    def test_identity_fields_validate_and_serialize(self):
        sp = server.cp.plan.SectionPlan("claude-api-key", "https://fixture.invalid",
                                        "fixture-identity-key", models=["claude-opus-4"])
        ident = SimpleNamespace(claude_cloak_modes=["always"],
                                claude_fingerprint_profiles=["claude-code-cli"])
        from cpa_probe import cpa_source_probe
        with patch.object(cpa_source_probe, "cached_identity", return_value=ident):
            server._apply_identity_override(sp, {
                "cloak_mode": "always", "fingerprint_profile": "claude-code-cli",
                "rebuild_mid_system": True, "disable_cooling": False})
            with self.assertRaises(ValueError):
                server._apply_identity_override(sp, {"disable_cooling": "false"})
            with self.assertRaises(ValueError):
                server._apply_identity_override(sp, {"cloak_mode": "unsupported"})
        p = SimpleNamespace(host="fixture.invalid", line_no=1, masked_key="***",
                            skipped={}, any_writable=True, sections={"claude-api-key": sp})
        data = server.plan_json(p)["sections"]["claude-api-key"]
        self.assertEqual(data["cloak_mode"], "always")
        self.assertEqual(data["fingerprint_profile"], "claude-code-cli")
        self.assertIs(data["rebuild_mid_system"], True)
        self.assertIs(data["disable_cooling"], False)
        self.assertIsInstance(data["highest_models"], list)
        self.assertIsInstance(data["model_provenance"], dict)

    def test_enable_compat_keeps_native_exclusions(self):
        raw = """openai-compatibility:
  - name: fixture
    base-url: https://fixture.invalid/v1
    disabled: true
    excluded-models: ["legacy,quoted", "hash#name"]
    api-key-entries:
      - api-key: fixture-compat-key
"""
        out, _, problems = bulk.apply_bulk(raw, [
            {"section": "openai-compatibility", "index": 0, "action": "enable"}])
        self.assertFalse(problems)
        e = yaml.safe_load(out)["openai-compatibility"][0]
        self.assertNotIn("disabled", e)
        self.assertEqual(e["excluded-models"], ["legacy,quoted", "hash#name"])

    def test_flow_exclusions_are_parsed_not_split_on_commas(self):
        raw = RAW.replace("excluded-models:\n      - old-model",
                          'excluded-models: ["legacy,quoted", "hash#name"]')
        out, _, problems = bulk.apply_bulk(raw, [
            {"section": "codex-api-key", "index": 0, "action": "disable"}])
        self.assertFalse(problems)
        self.assertEqual(yaml.safe_load(out)["codex-api-key"][0]["excluded-models"],
                         ["legacy,quoted", "hash#name", "*"])

    def test_unique_prefix_targets_one_key_only(self):
        sp = SimpleNamespace(section="codex-api-key", models=["gpt-5"],
                             prefix="fixture-one", api_key="fixture-pinned",
                             base_url="https://fixture.invalid/v1")
        cfg = {"codex-api-key": [{"prefix": sp.prefix, "api-key": sp.api_key,
                                  "base-url": sp.base_url, "models": [{"name": "gpt-5"}]}]}
        model, scope = server._verification_target(cfg, sp)
        self.assertEqual(model, "fixture-one/gpt-5")
        self.assertEqual(scope, "unique_prefix")
        cfg["codex-api-key"].append({**cfg["codex-api-key"][0], "api-key": "fixture-other"})
        self.assertEqual(server._verification_target(cfg, sp), ("gpt-5", "gateway_only"))

    def test_context_redaction_removes_secret_in_free_text(self):
        original = {"msg": "failure fixture-unstructured-secret",
                    "prompt_cache_key": False, "verify_total": 3}
        out = server._public_with_context(original, {
            "headers": {"X-Custom": "fixture-unstructured-secret"}})
        self.assertNotIn("fixture-unstructured-secret", json.dumps(out))
        self.assertIs(out["prompt_cache_key"], False)
        self.assertEqual(out["verify_total"], 3)
        self.assertIn("fixture-unstructured-secret", original["msg"])

    def test_startup_logs_never_print_token(self):
        output = io.StringIO()
        fake_server = SimpleNamespace(serve_forever=lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        fields = {key: getattr(server.Handler, key) for key in (
            "cfg_path", "token", "backup_dir", "accept_cpa_key", "cpa_url",
            "cpa_source_root", "cpa_source_remote", "cpa_source_ref", "drift_proxy",
            "trusted_proxy_peers")}
        with tempfile.TemporaryDirectory() as folder:
            cfg = Path(folder) / "fixture.yaml"
            cfg.write_text("{}\n")
            with patch.multiple(server.Handler, **fields), \
                    patch.object(server.sys, "argv", [
                        "server", "--config", str(cfg), "--token", "fixture-startup-secret",
                        "--no-cpa-key", "--no-drift-remote"]), \
                    patch.object(server.Handler, "_cpa_mgmt_hash", return_value=""), \
                    patch.object(server, "ThreadingHTTPServer", return_value=fake_server), \
                    contextlib.redirect_stdout(output):
                server.main()
        self.assertNotIn("fixture-startup-secret", output.getvalue())
        self.assertNotIn("?token=", output.getvalue())

    def test_confirmation_stays_responsive_while_commit_lock_is_held(self):
        store = server.Store()
        entry = {"preview": "{}\n", "base_raw": "{}\n", "plans": []}
        store.add_plan("fixture-plan", entry)
        h = handler("{}\n")
        completed = threading.Event()
        def confirm():
            h._api_apply({"plan_id": "fixture-plan", "confirm": True})
            completed.set()
        with patch.object(server, "STORE", store), \
                patch.object(server, "_start_apply_task"):
            with server.Handler._apply_lock:
                worker = threading.Thread(target=confirm)
                worker.start()
                responsive = completed.wait(1)
            worker.join(2)
        self.assertTrue(responsive)
        self.assertEqual(h.response[0], 202)
        self.assertEqual(h.response[1]["stage"], "queued")
        self.assertIs(h.response[1]["local_written"], False)

    def test_local_write_and_management_put_cannot_interleave(self):
        store = server.Store()
        entered = threading.Event()
        release = threading.Event()
        raw, first, second = "{}\n", "request-retry: 1\n", "request-retry: 2\n"
        a_entry = {"base_raw": raw, "preview": first}
        b_entry = {"base_raw": first, "preview": second}
        a, _ = store.claim_apply(a_entry)
        calls = []
        def reload(base, mgmt, text):
            calls.append(text)
            if text == first:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("fixture synchronization timed out")
            return True, "fixture"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.yaml"
            path.write_text(raw)
            with patch.object(server, "STORE", store), \
                    patch.object(server, "reload_cpa", side_effect=reload), \
                    patch.object(server.time, "sleep"):
                t1 = threading.Thread(target=server._commit_apply, args=(
                    a, a_entry, {}, str(path), "http://127.0.0.1:8317", "fixture-management", "", folder))
                t1.start()
                t2 = None
                try:
                    self.assertTrue(entered.wait(2))
                    b, _ = store.claim_apply(b_entry)
                    t2 = threading.Thread(target=server._commit_apply, args=(
                        b, b_entry, {}, str(path), "http://127.0.0.1:8317", "fixture-management", "", folder))
                    t2.start()
                    self.assertEqual(path.read_text(), first)
                    self.assertEqual(calls, [first])
                finally:
                    release.set()
                    t1.join(4)
                    if t2:
                        t2.join(4)
                self.assertFalse(t1.is_alive())
                self.assertFalse(t2.is_alive())
            self.assertEqual(calls, [first, second])
            self.assertEqual(path.read_text(), second)

    def test_capacity_rejection_does_not_claim_an_apply(self):
        store = server.Store()
        store.MAX_APPLIES = 1
        store.claim_apply({"preview": "{}"})
        entry = {"preview": "{}", "base_raw": "{}"}
        store.add_plan("fixture-plan", entry)
        h = handler("{}")
        with patch.object(server, "STORE", store), patch.object(server, "_start_apply_task") as start:
            h._api_apply({"plan_id": "fixture-plan", "confirm": True})
        self.assertEqual(h.response[0], 429)
        self.assertNotIn("task_id", entry)
        start.assert_not_called()

    def test_native_exclusion_first_field_is_supported(self):
        raw = """codex-api-key:
  - excluded-models:
      - old-model # keep-comment
    api-key: fixture-first-field
    base-url: https://fixture.invalid/v1
"""
        out, _, problems = bulk.apply_bulk(raw, [
            {"section": "codex-api-key", "index": 0, "action": "disable"}])
        self.assertFalse(problems)
        self.assertEqual(yaml.safe_load(out)["codex-api-key"][0]["excluded-models"],
                         ["old-model", "*"])
        self.assertIn("# keep-comment", out)

    def test_nonconflicting_operations_share_original_entry(self):
        ops = [
            {"section": "codex-api-key", "index": 0, "action": "disable"},
            {"section": "codex-api-key", "index": 0, "action": "priority", "value": 12}]
        out, _, problems = bulk.apply_bulk(RAW, ops)
        self.assertFalse(problems)
        entries = yaml.safe_load(out)["codex-api-key"]
        self.assertEqual(entries[0]["priority"], 12)
        self.assertIn("*", entries[0]["excluded-models"])
        self.assertNotIn("priority", entries[1])

    def test_redacted_header_roundtrip_preserves_internal_credentials(self):
        original = {"Authorization": "Bearer fixture-roundtrip-secret", "X-Channel": "fixture-old"}
        public = server._public(original, "headers")
        public["X-Channel"] = "fixture-new"
        merged = server._restore_public_headers(public, original)
        self.assertEqual(merged["Authorization"], original["Authorization"])
        self.assertEqual(merged["X-Channel"], "fixture-new")
        with self.assertRaises(ValueError):
            server._restore_public_headers({"X-New-Secret": "***"}, {})

    def test_redacted_proxy_roundtrip_preserves_internal_credentials(self):
        original = "http://fixture-user:fixture-pass@fixture.invalid:7890"
        public = server._public(original, "proxy_url")
        self.assertEqual(server._restore_public_scalar(public, original, "proxy_url"), original)

    def test_bulk_public_diff_is_redacted_internal_text_is_not(self):
        raw = RAW.replace("priority: 7", "priority: 0")
        h = handler(raw)
        h.wfile = io.BytesIO()
        h.send_response = lambda code: setattr(h, "status", code)
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h._json = server.Handler._json.__get__(h, server.Handler)
        store = server.Store()
        with patch.object(server, "STORE", store):
            h._api_bulk_preview({"revision": bulk.config_revision(raw), "ops": [
                {"section": "codex-api-key", "index": 0, "action": "delete"}]})
        public = h.wfile.getvalue().decode()
        self.assertEqual(h.status, 200)
        for secret in ("fixture-secret-one", "fixture-secret-two"):
            self.assertNotIn(secret, public)
        # 上游地址保留 —— 批量预览的用途就是「写回前看清要删/改哪个站」，
        # 域名抹掉就没法复核。私有域名的脱敏归提交那一层，见
        # test_public_output_keeps_types_and_redacts_credentials 的说明。
        self.assertIn("fixture.invalid", public)
        body = json.loads(public)
        internal = store.get_bulk(body["bulk_id"])
        self.assertIn("fixture-secret-two", internal["text"])
        self.assertIn("fixture-secret-one", internal["base_raw"])

    def test_txt_export_redacts_options_and_event_secrets(self):
        row = server.cp.parse_lines("https://fixture.invalid,fixture-export-key").valid[0]
        job = server.Job("fixture-export-job", [row], {
            "token": "fixture-option-secret",
            "proxy": "http://fixture-user:fixture-pass@fixture.invalid:7890"})
        job.emit("error", {"msg": "fixture-export-key fixture-option-secret"})
        store = server.Store()
        store.add_job(job)
        h = handler()
        h.wfile = io.BytesIO()
        h.send_response = lambda code: None
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        with patch.object(server, "STORE", store):
            h._api_export(job.id)
        public = h.wfile.getvalue().decode()
        for secret in ("fixture-export-key", "fixture-option-secret",
                       "fixture-user", "fixture-pass"):
            self.assertNotIn(secret, public)
        # 上游地址保留，且导出文件**自己写明了**这一点（server.py:2779
        # 「注：api-key 一律只出末四位。上游 URL 与模型名原样保留。」）。
        #
        # 这一行原来要求连域名一起抹。三个理由不改成那样：
        #   ① 导出的用途是排障 —— 事件流里哪个站超时、哪个站 403，抹掉站名
        #      就只剩一堆无主的错误；
        #   ② 文件头那句话会变成假的，而它是给接收方看的承诺；
        #   ③ docx 第 9 条管的是「提交到 GitHub」，那一层由 tools/scrub.py
        #      的 DOMAIN_MAP 负责（实测当前仓库 0 处真实域名）。
        # 导出给外人前若要连站名一起去掉，那是 export-logs.sh 打包那一步
        # 该做的事，不该让本地排障先失去信息。
        self.assertIn("fixture.invalid", public)

    def test_private_domains_are_scrubbed_before_publish_not_in_local_output(self):
        """docx 第 9 条的私有域名脱敏归**提交**这一层，不是本地响应这一层。

        2026-09-12 定的分层，两边各自钉住：

          · 本地 API 响应 / 导出：抹凭据，**保留**主机名 —— 界面与排障都靠
            站名工作，见 server._safe_text 与上面两个用例的说明。
          · 提交到 GitHub：`tools/scrub.py` 把真实域名换成占位符，配合
            .gitignore 排除不该上传的文件。

        这一项守的是第二层真的有效：仓库里不许出现真实私有域名。
        它同时是一道回归闸 —— 以后谁在源码或文档里粘了真实站名，这里会红。
        """
        root = Path(__file__).resolve().parents[1]
        scrub_py = root / "tools" / "scrub.py"
        self.assertTrue(scrub_py.is_file(), "提交前脱敏工具不在了")

        namespace: dict = {}
        exec(compile(scrub_py.read_text(encoding="utf-8"),
                     str(scrub_py), "exec"), namespace)
        # 映射表非空，且每一项都是「真实值 → 占位符」
        domain_map = namespace.get("DOMAIN_MAP") or []
        self.assertTrue(domain_map, "DOMAIN_MAP 空 —— 提交脱敏等于没做")
        for real, code in domain_map:
            self.assertTrue(real and code and real != code, (real, code))

        # 脱敏函数真的替换，而不是原样返回
        scrub = namespace["scrub"]
        sample_real = domain_map[0][0]
        out, count = scrub(f"base-url: https://{sample_real}/v1")
        self.assertNotIn(sample_real, out)
        self.assertGreaterEqual(count, 1)

    def test_inconsistent_final_preview_is_invalid_and_cannot_be_applied(self):
        row = server.cp.parse_lines("https://fixture.invalid/v1,fixture-plan-key").valid[0]
        sp = server.cp.plan.SectionPlan("codex-api-key", row.bare, row.api_key,
                                        models=["gpt-5"], priority=7)
        plan = SimpleNamespace(host=row.host, line_no=1, masked_key="***",
                               skipped={}, any_writable=True,
                               sections={"codex-api-key": sp})
        job = server.Job("fixture-job", [row], {})
        job.state = "done"
        job.results = [SimpleNamespace(row=row)]
        store = server.Store()
        store.add_job(job)
        h = handler(RAW)
        with patch.object(server, "STORE", store), \
                patch.object(server.cp, "build_plan", return_value=plan), \
                patch.object(server.cp, "mark_new_sections", return_value=0), \
                patch.object(server.cp, "assign_priorities", return_value=[]), \
                patch.object(server, "build_diffs", return_value=[]), \
                patch.object(server, "apply_diffs", return_value=RAW), \
                patch.object(server, "_start_apply_task") as start:
            h._api_plan({"job_id": job.id, "selected": [[1, "codex-api-key"]]})
            self.assertEqual(h.response[0], 200)
            self.assertIs(h.response[1]["valid"], False)
            self.assertEqual(h.response[1]["preview_kind"], "incremental")
            self.assertIn("unified_diff", h.response[1])
            pid = h.response[1]["plan_id"]
            h._api_apply({"plan_id": pid, "confirm": True})
            self.assertEqual(h.response[0], 400)
            self.assertEqual(h.response[1]["error_code"], "invalid_plan")
            start.assert_not_called()


def urllib_path(url):
    return server.urllib.parse.urlsplit(url).path


if __name__ == "__main__":
    unittest.main()
