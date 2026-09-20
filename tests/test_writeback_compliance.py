"""Offline writeback contracts: synthetic YAML, fake HTTP and temporary files."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from cpa_probe import writeback as wb
from cpa_probe.plan import ImportPlan, SectionPlan


def plan(section="claude-api-key", key="fixture-key-a", base="https://unit.example/A",
         **kwargs):
    return SectionPlan(section, base, key, models=["fixture-model"],
                       model_source="probed", **kwargs)


def rebuild(raw, *sections):
    plans = {}
    for sp in sections:
        p = ImportPlan("unit.example", "***", sections={sp.section: sp})
        plans[(sp.base_url, sp.api_key)] = p
    text, _ = wb.rebuild_config_full(yaml.safe_load(raw), plans,
                                     raw.splitlines(keepends=True))
    assert wb.validate(text)[0], "rendered YAML must be valid"
    return yaml.safe_load(text)


class FakeResponse(io.BytesIO):
    status = 200


class WritebackCompliance(unittest.TestCase):
    def test_new_fields_merge_once_and_false(self):
        sp = plan(cloak_mode="never", fingerprint_profile="new-profile",
                  rebuild_mid_system=False, disable_cooling=False)
        sp.carry_lines = [
            "    cloak: {mode: always, custom: {keep: [1, 2]}}",
            "    fingerprint-profile: old-profile",
            "    rebuild-mid-system-message: true",
            "    disable-cooling: true",
        ]
        text = "claude-api-key:\n" + "\n".join(wb.render_entry(sp, "  ", "    ", "test"))
        entry = yaml.safe_load(text)["claude-api-key"][0]
        self.assertEqual(entry["cloak"], {"mode": "never", "custom": {"keep": [1, 2]}})
        self.assertEqual(entry["fingerprint-profile"], "new-profile")
        self.assertIs(entry["rebuild-mid-system-message"], False)
        self.assertIs(entry["disable-cooling"], False)
        self.assertEqual(text.count("disable-cooling:"), 1)

    def test_unmeasured_fields_survive(self):
        raw = """claude-api-key:
  - api-key: fixture-key-a
    base-url: https://unit.example/A
    cloak: {mode: always, custom: [one]}
    fingerprint-profile: keep-profile
    rebuild-mid-system-message: true
    disable-cooling: true
"""
        entry = rebuild(raw, plan())["claude-api-key"][0]
        self.assertEqual(entry["cloak"]["custom"], ["one"])
        self.assertIs(entry["disable-cooling"], True)

    def test_duplicate_keys_rejected_including_flow(self):
        for text in ("a: 1\na: 2", "a: {b: 1, b: 2}",
                     'a: [{api-key: "fixture-secret", api-key: other}]'):
            with self.subTest(text=text):
                ok, message = wb.validate(text)
                self.assertFalse(ok)
                self.assertNotIn("fixture-secret", message)

    def test_yaml_merge_override_is_valid(self):
        self.assertTrue(wb.validate("a: &a {x: 1}\nb: {<<: *a, x: 2}")[0])
        self.assertTrue(wb.validate("a: &a {x: 1}\nb: &b {x: 2}\nc: {<<: [*a, *b]}")[0])

    def test_unselected_flow_and_default_base_survive(self):
        raw = """gemini-api-key: [{api-key: default-credential}]
claude-api-key: [{api-key: fixture-key-a, base-url: 'https://unit.example/A'}, {api-key: fixture-key-a, base-url: 'https://unit.example/B', disabled: true}]
"""
        cfg = rebuild(raw, plan(), plan("gemini-api-key", "new-key"))
        self.assertEqual(len(cfg["claude-api-key"]), 2)
        self.assertEqual(len(cfg["gemini-api-key"]), 2)
        self.assertIn({"api-key": "default-credential"}, cfg["gemini-api-key"])

    def test_block_channel_identity_and_no_host_carry(self):
        raw = """claude-api-key:
  - api-key: fixture-key-a
    base-url: https://unit.example/A
    disabled: true
    headers: {X-Channel: A}
  - api-key: fixture-key-a
    base-url: https://unit.example/B
    models: [{name: old-model}]
"""
        rows = rebuild(raw, plan(base="https://unit.example/B"))["claude-api-key"]
        self.assertEqual(len(rows), 2)
        target = next(r for r in rows if r["base-url"].endswith("/B"))
        self.assertNotIn("disabled", target)
        self.assertNotIn("headers", target)

    def test_numeric_and_boolean_config_values_are_not_masked(self):
        """键名含 credential / key 但值是数字或布尔时，不许当凭据脱敏。

        2026-09-12 现场：生产 config.yaml 的 `max-retry-credentials: 12` 在
        diff 里显示成 `12******`，`credential-concurrency.default: 4` 与
        `credential-in-flight.max: 2` 同样。成因是 `sensitive` 正则按**子串**
        匹配键名（`credential`、`key`），而那三个键的值是整数。

        后果不是显示难看而已：全局调优面板要让操作员核对「预算从 12 改成几」，
        而两侧都被打码时那个 diff 无法复核 —— 等于让人凭信任点确认写盘。

        判据用 PyYAML 解析出的**标签**：凭据一定是字符串（str 标签），
        而 `12` / `true` / `1.5` 解析成 int / bool / float。带引号的 `"12"`
        仍是 str，照旧脱敏 —— 那可能真是一把数字组成的 Key。
        """
        raw = ("max-retry-credentials: 12\n"
               "credential-concurrency:\n  default: 4\n"
               "credential-in-flight:\n  max: 2\n"
               "ws-auth: true\n"
               "claude-api-key:\n"
               "  - api-key: fixture-real-secret-here\n"
               "    numeric-key: 987500\n"
               '    quoted-key: "123456789012"\n')
        got = wb.redact_yaml_secrets(raw)
        for keep in ("max-retry-credentials: 12", "default: 4", "max: 2",
                     "ws-auth: true", "numeric-key: 987500"):
            self.assertIn(keep, got, f"数字/布尔被当凭据打码了：{keep}")
        self.assertNotIn("fixture-real-secret-here", got)
        self.assertNotIn("123456789012", got, "带引号的数字串仍要脱敏")

    def test_global_tuning_edits_values_and_keeps_every_comment(self):
        """全局调优只改值：键序、注释、缩进、行数逐字保留。

        这些键的注释是**决策记录**（`max-retry-credentials` 那条记着四次
        调值的实测依据与 CPA 源码行号）。改用 YAML dump 会重排键序并丢掉
        全部注释，等于把「为什么是这个数」永久删除 —— 所以走行级编辑。
        """
        from cpa_probe import tuning

        raw = ("debug: true  # 排障期开着\n"
               "# 决策记录：4 -> 9 -> 12，见 conductor_execution.go:325\n"
               "request-retry: 1\n"
               "max-retry-credentials: 12\n"
               "max-retry-interval: 8\n"
               "streaming:\n"
               "  keepalive-seconds: 15\n"
               "  bootstrap-retries: 1\n"
               "codex:\n"
               "  stream-bootstrap-buffering: false  # 保留这条\n"
               "  optimize-multi-agent-v2: false\n")
        advices = [
            tuning.Advice(("debug",), True, False, "why"),
            tuning.Advice(("streaming", "bootstrap-retries"), 1, 3, "why"),
            tuning.Advice(("codex", "stream-bootstrap-buffering"), False, True, "why"),
        ]
        diffs, problems = wb.global_tuning_diffs(raw, advices)
        self.assertEqual(problems, [])
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].replace_all)
        new = "\n".join(diffs[0].lines)

        self.assertEqual(len(new.split("\n")), len(raw.split("\n")), "行数变了")
        # 注释逐字保留（含行尾注释与独立注释行）
        for comment in ("# 排障期开着", "# 保留这条",
                        "# 决策记录：4 -> 9 -> 12，见 conductor_execution.go:325"):
            self.assertIn(comment, new, f"注释丢了：{comment}")
        # 键序不变
        def keys(text):
            return [ln.split(":")[0] for ln in text.split("\n")
                    if ln and not ln.lstrip().startswith("#")]
        self.assertEqual(keys(new), keys(raw), "键序被重排")
        # 值真的改了，且没碰同块的邻居
        loaded = yaml.safe_load(new)
        self.assertIs(loaded["debug"], False)
        self.assertEqual(loaded["streaming"]["bootstrap-retries"], 3)
        self.assertIs(loaded["codex"]["stream-bootstrap-buffering"], True)
        self.assertEqual(loaded["streaming"]["keepalive-seconds"], 15)
        self.assertIs(loaded["codex"]["optimize-multi-agent-v2"], False)

    def test_global_tuning_reports_keys_it_cannot_edit(self):
        """改不动的键要报出来，不许静默跳过。

        静默跳过的后果：界面显示「已应用」而那一项其实没改，操作员以为
        问题解决了，524 / 空 200 继续发生却再也不会去看这一项。
        """
        from cpa_probe import tuning

        raw = "codex:\n  live-media-relay:\n    enabled: false\n"
        # 三层深的路径本模块不处理；块值也不能当标量改
        advices = [
            tuning.Advice(("codex", "live-media-relay", "enabled"), False, True, "w"),
            tuning.Advice(("codex", "live-media-relay"), None, True, "w"),
            tuning.Advice(("nonexistent-key",), 1, 2, "w"),
        ]
        diffs, problems = wb.global_tuning_diffs(raw, advices)
        self.assertEqual(diffs, [])
        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(any("找不到" in p for p in problems), problems)
        self.assertTrue(any("不是标量" in p for p in problems), problems)

    def test_url_path_case_is_identity(self):
        self.assertNotEqual(wb.compat_provider_key("https://unit.example/A"),
                            wb.compat_provider_key("https://unit.example/a"))

    def test_quoted_scalar_roundtrip(self):
        for value in ("fixture\\key", 'fixture"key', "fixture'key", "fixture\\"):
            self.assertEqual(wb._scalar_value(wb._yaml_str(value) + " # note"), value)
        self.assertEqual(wb._scalar_value("'fixture''key'"), "fixture'key")

    def test_escaped_credentials_not_duplicated(self):
        key = "fixture\\key"
        raw = "claude-api-key:\n  - api-key: " + wb._yaml_str(key) + "\n    base-url: https://unit.example/A\n"
        self.assertEqual(len(rebuild(raw, plan(key=key))["claude-api-key"]), 1)

    def test_compat_split_capabilities_and_keep_orphan(self):
        raw = """openai-compatibility:
  - name: fixture-provider
    base-url: https://unit.example/A
    priority: 1
    disable-cooling: true
    custom: {deep: [{keep: yes}]}
    headers: {X-Channel: old}
    models: [{name: old-model, alias: old-alias}]
    api-key-entries:
      - api-key: fixture-key-a
        proxy-url: http://proxy.example:8080
        weight: 0
      - api-key: fixture-key-b
        weight: 2
      - api-key: fixture-key-unselected
        weight: 3
"""
        a = plan("openai-compatibility", priority=100, headers={"X-Channel": "a"},
                 disable_cooling=False)
        b = plan("openai-compatibility", key="fixture-key-b", priority=100,
                 headers={"X-Channel": "b"})
        b.models = ["other-model"]
        rows = rebuild(raw, a, b)["openai-compatibility"]
        # 2026-09-20 改契约：同一个 base-url 只写**一条** provider，不再按
        # headers 差异拆成多条带哈希后缀的 provider（那会把同一个站在 CPA 里
        # 注册成 N 个独立 provider，冷却 / 模型能力 / 执行路由各走各的）。
        # 组内各 Key 探到的模型取并集，provider 级字段取 head（priority 最高
        # 那把）的；没进方案的 Key 原样留在同一条 provider 下。
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "fixture-provider")
        by_key = {k["api-key"]: k for k in row["api-key-entries"]}
        self.assertEqual(set(by_key), {a.api_key, b.api_key, "fixture-key-unselected"})
        self.assertEqual({m["name"] for m in row["models"]}, {"fixture-model", "other-model"})
        self.assertIs(row["disable-cooling"], False)
        self.assertEqual(by_key[a.api_key]["weight"], 0)
        self.assertEqual(by_key[a.api_key]["proxy-url"], "http://proxy.example:8080")
        self.assertEqual(by_key[b.api_key]["weight"], 2)
        self.assertEqual(by_key["fixture-key-unselected"]["weight"], 3)
        self.assertEqual(row["priority"], 100)
        self.assertIn("custom", row)

    def test_compat_incremental_split_and_weight_zero(self):
        a = plan("openai-compatibility", weight=0, disable_cooling=False)
        b = plan("openai-compatibility", key="fixture-key-b", headers={"X-Channel": "b"})
        b.models = ["other-model"]
        plans = [ImportPlan("unit.example", "***", sections={s.section: s}) for s in (a, b)]
        raw = "openai-compatibility: []\n"
        cfg = yaml.safe_load(wb.apply_diffs(raw, wb.build_diffs(raw, plans)))
        rows = cfg["openai-compatibility"]
        # 2026-09-20 改契约：同 base-url 的 Key 合进同一条 provider（见上一个
        # 用例的说明）。per-key 的 weight 仍逐把保留，模型取并集。
        self.assertEqual(len(rows), 1)
        row = rows[0]
        by_key = {k["api-key"]: k for k in row["api-key-entries"]}
        self.assertEqual(set(by_key), {a.api_key, b.api_key})
        self.assertEqual(by_key[a.api_key]["weight"], 0)
        self.assertNotIn("weight", by_key[b.api_key])
        self.assertIs(row["disable-cooling"], False)
        self.assertEqual({m["name"] for m in row["models"]}, {"fixture-model", "other-model"})

    def test_multiple_model_aliases_survive(self):
        raw = """claude-api-key:
  - api-key: fixture-key-a
    base-url: https://unit.example/A
    models:
      - {name: fixture-model, alias: alias-one, custom: [1]}
      - {name: fixture-model, alias: alias-two, custom: [2]}
"""
        models = rebuild(raw, plan())["claude-api-key"][0]["models"]
        self.assertEqual({m["alias"] for m in models}, {"alias-one", "alias-two"})
        self.assertEqual([m["custom"] for m in models], [[1], [2]])

    def test_structural_redaction_scrubs_comments_and_keeps_metadata(self):
        raw = """api-keys: # clients
- fixture-client
claude-api-key: [{api-key: fixture-native, base-url: 'https://user-fixture:pass-fixture@unit.example/A?token=query-fixture&region=west', priority: 100, headers: {Authorization: 'Bearer auth-fixture', X-Custom-Secret: header-fixture, User-Agent: useful-agent}}]
openai-compatibility: [{api-key-entries: [{api-key: fixture-compat}]}]
# copies: fixture-client fixture-native fixture-compat auth-fixture header-fixture query-fixture user-fixture pass-fixture
"""
        result = wb.redact_yaml_secrets(raw)
        for secret in ("fixture-client", "fixture-native", "fixture-compat", "auth-fixture",
                       "header-fixture", "query-fixture", "pass-fixture"):
            self.assertNotIn(secret, result)
        self.assertIn("useful-agent", result)
        self.assertIn("priority: 100", result)
        self.assertIn("region=west", result)
        # URL userinfo: the password is scrubbed, the username is kept on
        # purpose. It is the only thing that identifies which proxy or
        # gateway credential a line belongs to, and the redacted diff exists
        # so the operator can tell the entries apart before writing back.
        # tests/test_server.py pins the same contract for the live diff
        # endpoint ("http://user:***@mihomo:7890").
        self.assertIn("user-fixture", result)

    def test_semantic_readback_not_counts(self):
        want = "claude-api-key: [{api-key: fixture-a, base-url: 'https://unit.example/A', priority: 10, headers: {X-Channel: A}, models: [{name: m}]}]"
        for before, after in (("priority: 10", "priority: 20"), ("/A", "/B"),
                              ("fixture-a", "fixture-b"), ("X-Channel: A", "X-Channel: B"),
                              ("name: m", "name: n")):
            got = want.replace(before, after)
            with patch.object(wb.urllib.request, "urlopen", return_value=FakeResponse(got.encode())):
                self.assertFalse(wb._readback_check("https://unit.example", "fixture-mgmt", want)[0])
        got = yaml.safe_dump(yaml.safe_load(want), sort_keys=True)
        with patch.object(wb.urllib.request, "urlopen", return_value=FakeResponse(got.encode())):
            self.assertTrue(wb._readback_check("https://unit.example", "fixture-mgmt", want)[0])

    def test_verify_rejects_200_non_results(self):
        bodies = ("", "{}", "<html>maintenance</html>", '{"error":{"message":"fixture-client"}}',
                  'data: {"type":"response.created"}\n\n',
                  'event: error\ndata: {"error":{"message":"failed"}}\n\n',
                  'data: {"type":"response.failed"}\n\n')
        for body in bodies:
            with self.subTest(body=body), patch("cpa_probe.client.send", return_value=SimpleNamespace(status="200", body=body)):
                ok, message = wb.verify_upstream("https://unit.example", "fixture-client", "codex-api-key", "fixture-model")
                self.assertFalse(ok)
                self.assertNotIn("fixture-client", message)

    def test_verify_valid_result_is_gateway_scope(self):
        body = json.dumps({"model": "fixture-model", "content": [{"type": "text", "text": "result"}]})
        with patch("cpa_probe.client.send", return_value=SimpleNamespace(status="200", body=body)):
            ok, message = wb.verify_upstream("https://unit.example", "fixture-client", "claude-api-key", "fixture-model")
        self.assertTrue(ok)
        self.assertIn("gateway", message)

    def test_backup_fsync_precedes_live_write_and_inode_survives(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            inode = path.stat().st_ino
            calls = []
            real_sync = os.fsync
            def sync(fd):
                calls.append(path.read_text(encoding="utf-8"))
                return real_sync(fd)
            with patch.object(wb.os, "fsync", side_effect=sync):
                bak = wb.write_local(str(path), "a: 2\n")
            self.assertEqual(calls[0], "a: 1\n")
            self.assertEqual(Path(bak).read_text(encoding="utf-8"), "a: 1\n")
            self.assertEqual(path.stat().st_ino, inode)

    def test_write_conflict_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            with self.assertRaises(Exception):
                wb.write_local(str(path), "a: 2\n", expected_version="stale")
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 1\n")

    def test_partial_write_reports_recoverable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            real_write = os.write
            failed = False
            def broken(fd, data):
                nonlocal failed
                if data == b"a: 2\n" and not failed:
                    failed = True
                    real_write(fd, data[:2])
                    raise OSError("injected failure")
                return real_write(fd, data)
            with patch.object(wb.os, "write", side_effect=broken):
                with self.assertRaises(OSError) as ctx:
                    wb.write_local(str(path), "a: 2\n")
            bak = getattr(ctx.exception, "backup_path", None)
            self.assertIsNotNone(bak)
            self.assertEqual(Path(bak).read_text(encoding="utf-8"), "a: 1\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 1\n")

    def test_incremental_flow_upsert_does_not_duplicate_credentials(self):
        raw = "claude-api-key: [{api-key: fixture-key-a, base-url: 'https://unit.example/A'}, {api-key: fixture-other}]\n"
        sp = plan(disable_cooling=False)
        p = ImportPlan("unit.example", "***", sections={sp.section: sp})
        text = wb.apply_diffs(raw, wb.build_diffs(raw, [p]))
        self.assertTrue(wb.validate(text)[0])
        rows = yaml.safe_load(text)["claude-api-key"]
        self.assertEqual(len(rows), 2)
        self.assertIs(rows[0]["disable-cooling"], False)

    def test_selected_unknown_comments_survive(self):
        raw = """claude-api-key:
  - api-key: fixture-key-a
    base-url: https://unit.example/A
    custom:
      # preserve this nested diagnostic
      setting: 12 # keep inline note
"""
        sp = plan()
        p = ImportPlan("unit.example", "***", sections={sp.section: sp})
        text, _ = wb.rebuild_config_full(yaml.safe_load(raw), {(sp.base_url, sp.api_key): p},
                                          raw.splitlines(True))
        self.assertIn("# preserve this nested diagnostic", text)
        self.assertIn("# keep inline note", text)

    def test_semantic_readback_distinguishes_boolean_from_number(self):
        with patch.object(wb.urllib.request, "urlopen", return_value=FakeResponse(b"setting: true")):
            self.assertFalse(wb._readback_check("https://unit.example", "fixture-mgmt", "setting: 1")[0])

    def test_url_identity_keeps_userinfo_case(self):
        self.assertNotEqual(wb.compat_provider_key("https://User:Secret@UNIT.example/A"),
                            wb.compat_provider_key("https://User:secret@unit.example/A"))

    def test_verify_rejects_empty_content_shapes(self):
        for obj in ({"content": [{}]}, {"output": [{}]}, {"candidates": [{"content": {"parts": [{}]}}]}):
            with patch("cpa_probe.client.send", return_value=SimpleNamespace(status="200", body=json.dumps(obj))):
                self.assertFalse(wb.verify_upstream("https://unit.example", "fixture-client", "claude-api-key", "fixture-model")[0])

    def test_partial_failure_does_not_restore_over_external_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            real_write = os.write
            def concurrent(fd, data):
                real_write(fd, b"a: 99\n")
                raise OSError("external writer")
            with patch.object(wb.os, "write", side_effect=concurrent):
                with self.assertRaises(wb.WritebackError) as ctx:
                    wb.write_local(str(path), "a: 2\n")
            self.assertFalse(ctx.exception.restored)
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 99\n")
            self.assertEqual(Path(ctx.exception.backup_path).read_text(encoding="utf-8"), "a: 1\n")

    def test_backup_sync_failure_never_touches_live_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            with patch.object(wb.os, "fsync", side_effect=OSError("injected sync failure")):
                with self.assertRaises(OSError):
                    wb.write_local(str(path), "a: 2\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 1\n")

    def test_successful_expected_version_and_scope_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            version = wb.config_version(str(path))
            wb.write_local(str(path), "a: 2\n", expected_version=version)
            self.assertNotEqual(wb.config_version(str(path)), version)
        scope = {}
        response = SimpleNamespace(status="200", body='{"content":[{"type":"text","text":"result"}]}')
        with patch("cpa_probe.client.send", return_value=response):
            self.assertTrue(wb.verify_upstream("https://unit.example", "fixture-client", "claude-api-key", "fixture-model", scope=scope)[0])
        self.assertEqual(scope, {"verification_scope": "gateway", "target_verified": False})

    def test_merge_alias_source_preserves_semantics(self):
        raw = """defaults: &defaults
  custom: {list: [one, two]}
  disable-cooling: true
claude-api-key:
  - <<: *defaults
    api-key: fixture-key-a
    base-url: https://unit.example/A
"""
        entry = rebuild(raw, plan(disable_cooling=False))["claude-api-key"][0]
        self.assertEqual(entry["custom"], {"list": ["one", "two"]})
        self.assertIs(entry["disable-cooling"], False)

    def test_distinct_compat_channels_have_distinct_names(self):
        a = plan("openai-compatibility")
        b = plan("openai-compatibility", key="fixture-key-b", base="https://unit.example/B")
        rows = rebuild("openai-compatibility: []\n", a, b)["openai-compatibility"]
        self.assertEqual(len({r["name"] for r in rows}), 2)

    def test_incremental_split_priorities_align(self):
        a = plan("openai-compatibility", priority=10)
        b = plan("openai-compatibility", key="fixture-key-b", priority=20,
                 headers={"X-Channel": "B"})
        plans = [ImportPlan("unit.example", "***", sections={s.section: s}) for s in (a, b)]
        raw = "openai-compatibility: []\n"
        rows = yaml.safe_load(wb.apply_diffs(raw, wb.build_diffs(raw, plans)))["openai-compatibility"]
        self.assertEqual({r["priority"] for r in rows}, {20})

    def test_verification_valid_and_incomplete_sse(self):
        completed = 'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"result"}]}]}}\n\n'
        delta = 'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"result"}}\n\n'
        for section, body, expected in (
            ("codex-api-key", completed, True),
            ("claude-api-key", delta, False),
            ("claude-api-key", delta + 'event: message_stop\ndata: {"type":"message_stop"}\n\n', True),
            ("codex-api-key", 'data: {"type":"response.output_text.delta","delta":"result"}\n\ndata: [DONE]\n\n', False),
        ):
            with self.subTest(section=section, expected=expected), patch(
                    "cpa_probe.client.send", return_value=SimpleNamespace(status="200", body=body)):
                self.assertEqual(wb.verify_upstream("https://unit.example", "fixture-client",
                                                   section, "fixture-model")[0], expected)

    def test_merge_keyword_is_not_the_merge_operator(self):
        self.assertTrue(wb.validate("defaults: &d {x: 1}\nvalue: {<<: *d, merge: ordinary}")[0])

    def test_flow_span_respects_yaml_escaped_quotes(self):
        raw = 'claude-api-key: [\n{api-key: "fixture\\"}]",\nbase-url: "https://unit.example/A"}\n]\nother: true\n'
        self.assertEqual(wb._flow_section_span(raw.splitlines(True), 0), 4)

    def test_compat_split_rebuild_is_semantically_idempotent(self):
        a = plan("openai-compatibility", headers={"X-Channel": "a"}, priority=100)
        b = plan("openai-compatibility", key="fixture-key-b", headers={"X-Channel": "b"}, priority=100)
        raw = "openai-compatibility: [{name: fixture-provider, base-url: 'https://unit.example/A', api-key-entries: [{api-key: fixture-key-a}, {api-key: fixture-key-b}]}]"
        first = rebuild(raw, a, b)
        second = rebuild(yaml.safe_dump(first), a, b)
        self.assertEqual(first, second)

    def test_backup_time_conflict_keeps_newer_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            real_backup = wb.backup
            def concurrent(*args, **kwargs):
                result = real_backup(*args, **kwargs)
                path.write_text("a: 99\n", encoding="utf-8")
                return result
            with patch.object(wb, "backup", side_effect=concurrent):
                with self.assertRaises(wb.WritebackError) as ctx:
                    wb.write_local(str(path), "a: 2\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "a: 99\n")
            self.assertTrue(Path(ctx.exception.backup_path).is_file())

    def test_unselected_merge_record_survives_selected_anchor(self):
        raw = """claude-api-key:
  - &native
    api-key: fixture-key-a
    base-url: https://unit.example/A
    custom: {keep: true}
  - <<: *native
    api-key: fixture-key-b
"""
        rows = rebuild(raw, plan())["claude-api-key"]
        other = next(r for r in rows if r["api-key"] == "fixture-key-b")
        self.assertEqual(other, {"api-key": "fixture-key-b",
                                 "base-url": "https://unit.example/A", "custom": {"keep": True}})

    def test_network_exception_does_not_expose_credential(self):
        with patch("cpa_probe.client.send", side_effect=OSError("fixture-client")):
            ok, message = wb.verify_upstream("https://unit.example", "fixture-client",
                                             "claude-api-key", "fixture-model")
        self.assertFalse(ok)
        self.assertNotIn("fixture-client", message)

    # ── request-scoped-errors：新条目必须带上本部署的策略 ────────────
    #
    # 2026-09-13 现场：render_entry 是白名单渲染，白名单里没有
    # request-scoped-errors；carry_lines 又只对既有条目有值。于是原文件没有的
    # (凭据,段) 写出来一条规则都不带 —— 而那个块是「上游回余额不足/被封/
    # CF 挑战就立刻跳下一个凭据」的唯一开关（action: continue-and-cooldown）。
    # 生产 config.yaml 162 个条目里 116 个带它，新站不带等于被排除在容灾外：
    # 那把 Key 没钱了 CPA 会一直重试它而不降级。

    def test_learn_scoped_error_rules_needs_a_majority(self):
        # 出现一次的是特例，不推广
        one = {"claude-api-key": [
            {"api-key": "k1", "base-url": "https://a.example",
             "request-scoped-errors": [{"status": 403, "action": "continue"}]},
            {"api-key": "k2", "base-url": "https://b.example"},
            {"api-key": "k3", "base-url": "https://c.example"},
        ]}
        self.assertEqual(wb.learn_scoped_error_rules(one, "claude-api-key"), [])
        # 占多数才认
        rule = [{"status": 403, "match": ["quota_exceeded"],
                 "action": "continue-and-cooldown"}]
        many = {"claude-api-key": [
            {"api-key": f"k{i}", "base-url": f"https://s{i}.example",
             "request-scoped-errors": rule} for i in range(3)
        ]}
        self.assertEqual(wb.learn_scoped_error_rules(many, "claude-api-key"), rule)
        # 两种形状各半 —— 谁都不占多数，不推广（避免把特例混成谁都不对的规则）
        other = [{"status": 400, "action": "stop"}]
        tie = {"claude-api-key": [
            {"api-key": "k1", "base-url": "https://a.example",
             "request-scoped-errors": rule},
            {"api-key": "k2", "base-url": "https://b.example",
             "request-scoped-errors": rule},
            {"api-key": "k3", "base-url": "https://c.example",
             "request-scoped-errors": other},
            {"api-key": "k4", "base-url": "https://d.example",
             "request-scoped-errors": other},
        ]}
        self.assertEqual(wb.learn_scoped_error_rules(tie, "claude-api-key"), [])
        # 缺段 / 非法结构不抛
        self.assertEqual(wb.learn_scoped_error_rules({}, "claude-api-key"), [])
        self.assertEqual(
            wb.learn_scoped_error_rules({"claude-api-key": "nope"}, "claude-api-key"), [])

    def test_new_entry_carries_learned_scoped_error_rules(self):
        rule = [{"status": 403, "match": ["quota_exceeded"],
                 "action": "continue-and-cooldown"}]
        sp = plan(scoped_error_rules=rule)
        rows = rebuild("claude-api-key: []\n", sp)["claude-api-key"]
        self.assertEqual(rows[0]["request-scoped-errors"], rule)

    def test_existing_entry_does_not_duplicate_scoped_error_rules(self):
        # 既有条目的规则已在 carry_lines 里逐字保真；再写一遍就是重复键，
        # UniqueLoader 会拒掉整份文件。
        rule = [{"status": 403, "match": ["quota_exceeded"],
                 "action": "continue-and-cooldown"}]
        sp = plan(carry_lines=wb._dump_fields(
                      {"request-scoped-errors": rule}, "    "),
                  scoped_error_rules=rule)
        text = "\n".join(wb.render_entry(sp, "  - ", "    ", "stamp"))
        self.assertEqual(text.count("request-scoped-errors:"), 1)
        rows = rebuild("claude-api-key: []\n", sp)["claude-api-key"]
        self.assertEqual(rows[0]["request-scoped-errors"], rule)

    def test_compat_provider_also_carries_scoped_error_rules(self):
        rule = [{"status": 403, "action": "continue-and-cooldown"}]
        sp = plan("openai-compatibility", scoped_error_rules=rule)
        rows = rebuild("openai-compatibility: []\n", sp)["openai-compatibility"]
        self.assertEqual(rows[0]["request-scoped-errors"], rule)

    def test_readback_rejects_non_mapping(self):
        with patch.object(wb.urllib.request, "urlopen", return_value=FakeResponse(b"[]")):
            self.assertFalse(wb._readback_check("https://unit.example", "fixture-mgmt", "[]")[0])

    # ── 探测可用 → 清掉原条目的停用标记（用户 2026-09-16）──────────────
    #
    # 用户原话：「无论原来是否被关闭，如果探测可用就要打开」。
    #
    # 这一组必须走**完整写回**（`_prepare_source_plan` → `attach_carry` →
    # render），不能只测 `_drop_disabling_lines`：实测踩到的两个 bug 都在
    # 装配那一层，函数本身是对的。
    #   ① `attach_carry` 用 `if sp.carry_lines:` 判断「补过了没」，而重开时
    #      carry 被**故意清空** —— 判断认为没补，把 `excluded-models: ["*"]`
    #      整份加了回来。现在用 `carry_attached` 区分「空」与「没补」。
    #   ② PyYAML 写列表用块序列（序列项与父键**同级缩进**），按「缩进更深」
    #      收集续行会让 `- '*'` 落单，写回后留下值为 null 的空
    #      `excluded-models:` —— 没重开成功还多一处 diff。

    def test_reenable_clears_excluded_models_and_keeps_other_fields(self):
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    prefix: "KEEP"
    priority: 10
    excluded-models: ["*"]
    headers:
      x-keep: "yes"
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  prefix="KEEP", reenable_fields=["excluded-models"])
        row = rebuild(raw, sp)["claude-api-key"][0]
        self.assertNotIn("excluded-models", row, row)
        # 其余字段与值一个不能动 —— 清停用标记不是「重建条目」
        self.assertEqual(row["prefix"], "KEEP")
        self.assertEqual(row["headers"], {"x-keep": "yes"})

    def test_reenable_keeps_operator_exclusions_other_than_star(self):
        """只摘 `*`，操作员手工排除的模型名要留着。

        `excluded-models: ["*", "gpt-4"]` 里 `*` 是停用、`gpt-4` 是「别调度
        这个模型」—— 两个独立语义。整行删掉会把后者也弄丢。
        """
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    priority: 10
    excluded-models: ["*", "gpt-4"]
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  reenable_fields=["excluded-models"])
        row = rebuild(raw, sp)["claude-api-key"][0]
        self.assertEqual(row["excluded-models"], ["gpt-4"], row)

    def test_reenable_clears_block_sequence_excluded_models(self):
        """块序列写法的 `excluded-models` 也要清干净 —— 而且必须是 PyYAML 那种缩进。

        这条用例的 YAML 形态是**关键**，不能随手写成「序列项比父键更深」：
        PyYAML 对列表写块序列时，**序列项与父键同级**：

            excluded-models:
            - '*'

        而 `excluded-models` 不在 `_RENDERED_KEYS` 里，既有条目于是走 carry，
        carry 正是 `_dump_fields`（PyYAML）生成的 —— 所以这是**真实路径的
        常态形态**。按「缩进必须更深」收集续行会让 `- '*'` 落单，写回后留下
        一个值为 null 的空 `excluded-models:`：既没重开成功，还凭空多一处 diff。

        验证方法：把收集续行的判据改回「缩进 <= 条目级即停」，只有这条用例
        会红 —— 手写深缩进那种形态两条路都对，抓不住这个 bug（实测）。
        """
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    priority: 10
    excluded-models:
    - '*'
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  reenable_fields=["excluded-models"])
        row = rebuild(raw, sp)["claude-api-key"][0]
        self.assertNotIn("excluded-models", row, row)

    def test_reenable_block_sequence_keeps_operator_exclusions(self):
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    priority: 10
    excluded-models:
    - '*'
    - gpt-4
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  reenable_fields=["excluded-models"])
        row = rebuild(raw, sp)["claude-api-key"][0]
        self.assertEqual(row["excluded-models"], ["gpt-4"], row)

    def test_reenable_compat_clears_boolean_disabled(self):
        raw = """openai-compatibility:
  - name: "keep-me"
    base-url: "https://unit.example/C"
    disabled: true
    api-key-entries:
      - api-key: "unit-key"
"""
        sp = plan("openai-compatibility", key="unit-key",
                  base="https://unit.example/C", reenable_fields=["disabled"])
        row = rebuild(raw, sp)["openai-compatibility"][0]
        self.assertNotIn("disabled", row, row)
        self.assertEqual(row["name"], "keep-me", row)

    def test_no_reenable_when_probe_failed(self):
        """没探通就**不能**打开 —— 否则会把一个仍然不可用的站塞回调度池。"""
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    priority: 10
    excluded-models: ["*"]
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  reenable_fields=[])
        row = rebuild(raw, sp)["claude-api-key"][0]
        self.assertEqual(row["excluded-models"], ["*"], row)

    def test_reenable_does_not_touch_unrelated_entries(self):
        """清停用标记只作用于被重开的那一条，同段别的条目逐字保留。"""
        raw = """claude-api-key:
  - api-key: "unit-key"
    base-url: "https://unit.example/A"
    priority: 10
    excluded-models: ["*"]
  - api-key: "other-key"
    base-url: "https://other.example/A"
    priority: 20
    excluded-models: ["*"]
"""
        sp = plan(key="unit-key", base="https://unit.example/A",
                  reenable_fields=["excluded-models"])
        rows = rebuild(raw, sp)["claude-api-key"]
        mine = next(r for r in rows if r["api-key"] == "unit-key")
        other = next(r for r in rows if r["api-key"] == "other-key")
        self.assertNotIn("excluded-models", mine, mine)
        self.assertEqual(other["excluded-models"], ["*"], other)


if __name__ == "__main__":
    unittest.main()
