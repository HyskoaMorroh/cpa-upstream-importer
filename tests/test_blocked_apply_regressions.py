"""Blocked live acceptance must never be reported as a successful write."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from cpa_probe.writeback import (blocked_plan_warning, configuration_semantically_equal,
                                 _restore_section_order, _preserve_source_comments)


class BlockedApplyRegression(unittest.TestCase):
    def test_changed_inline_comment_retained_without_scalar_hashes(self):
        source = ['- api-key: fake # account note', '  priority: 1 # priority note',
                  '  label: "# scalar data"', '  note: | # block header note',
                  '    # block scalar data']
        rendered = ['- api-key: fake', '  priority: 2', '  label: changed', '  note: changed']
        result = '\n'.join(_preserve_source_comments(source, rendered, '  '))
        self.assertIn('# account note', result)
        self.assertIn('# priority note', result)
        self.assertIn('# block header note', result)
        self.assertNotIn('# scalar data', result)
        self.assertNotIn('# block scalar data', result)

    def test_inline_history_does_not_change_final_block_scalar_chomping(self):
        source = ['- api-key: fake # account note', '  priority: 1 # priority note',
                  '  body: | # header note', '    # literal body']
        rendered = ['- api-key: fake', '  priority: 2', '  body: |', '    # literal body']
        result = _preserve_source_comments(source, rendered, '  ')
        self.assertIn('  # priority note', result)
        self.assertTrue(configuration_semantically_equal('\n'.join(result), '\n'.join(rendered)))

    def test_ordered_semantics_reject_format_only_but_not_real_changes(self):
        self.assertTrue(configuration_semantically_equal("a: 1\n", "# note\na: 1\n"))
        self.assertFalse(configuration_semantically_equal("a: [1, 2]\n", "a: [2, 1]\n"))
        self.assertFalse(configuration_semantically_equal("a: 1\n", "a: true\n"))
        self.assertFalse(configuration_semantically_equal("a: [", "a: ["))

    def test_section_reorder_preserves_preamble_and_unchanged_bytes(self):
        source = ["claude-api-key:", "  # operator note", "  - api-key: first",
                  "    base-url: https://one.example", "    priority: 1",
                  "  - api-key: second", "    base-url: https://two.example", "    priority: 2"]
        generated = source[5:] + source[2:5]
        self.assertEqual(_restore_section_order(source, "claude-api-key", generated), source[1:])
        generated[2] = "    priority: 3"
        result = _restore_section_order(source, "claude-api-key", generated)
        self.assertEqual(result[:5], source[1:6])
        self.assertEqual(result[-1], "    priority: 3")

    def test_compat_order_and_new_native_rows(self):
        source = ["openai-compatibility:", "  # provider note", "  - name: first",
                  "    base-url: https://one.example", "    api-key-entries: [{api-key: a}]",
                  "  - name: second", "    base-url: https://two.example",
                  "    api-key-entries: [{api-key: b}]"]
        self.assertEqual(_restore_section_order(source, "openai-compatibility",
                                               source[5:] + source[2:5]), source[1:])
        native = ["claude-api-key:", "  - api-key: first", "    priority: 1"]
        result = _restore_section_order(native, "claude-api-key",
                                        ["  - api-key: new", "    priority: 2"] + native[1:])
        self.assertEqual(result, native[1:] + ["  - api-key: new", "    priority: 2"])

    def test_edited_duplicate_does_not_steal_unchanged_sibling(self):
        source = ["claude-api-key:", "  - api-key: shared", "    priority: 1",
                  "  - api-key: shared", "    priority: 2"]
        result = _restore_section_order(source, "claude-api-key",
                                        source[3:] + ["  - api-key: shared", "    priority: 3"])
        self.assertEqual(result, ["  - api-key: shared", "    priority: 3"] + source[3:])

    def test_warning_counts_actual_policy_and_evidence_blocks(self):
        sections = {str(i): SimpleNamespace(write_blocked=blocked, probe_policy_code=code)
                    for i, (blocked, code) in enumerate(
                        [(True, "provider_stopped"), (True, ""), (False, "")])}
        warning = blocked_plan_warning([SimpleNamespace(sections=sections)])
        self.assertIn("探测策略阻断 1 个", warning)
        self.assertIn("其他写回证据不足 1 个", warning)
        self.assertIn("手填模型清单不能替代", warning)
        self.assertNotIn("即可放行", warning)

    def test_noop_rejected_before_task_claim(self):
        replies = []
        handler = SimpleNamespace(_json=lambda status, body: replies.append((status, body)))
        with patch.object(server.STORE, "claim_apply") as claim:
            server.Handler._submit_apply(handler, {"preview": "# formatting only\na: 1\n", "base_raw": "a: 1\n", "valid": True}, {})
        claim.assert_not_called()
        self.assertEqual(replies[0][0], 400)
        self.assertEqual(replies[0][1]["error_code"], "no_configuration_changes")

    def test_queued_noop_cannot_write_or_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("a: 1\n", encoding="utf-8")
            task = SimpleNamespace(result={}, generation=server.STORE.apply_generation,
                                   set_stage=lambda stage: None)
            with patch.object(server, "write_local") as write, patch.object(server, "_run_apply_tail") as tail:
                server._commit_apply(task, {"preview": "# formatting only\na: 1\n", "base_raw": "a: 1\n"},
                                     {}, str(config), "", "", "")
            write.assert_not_called()
            tail.assert_not_called()
            self.assertEqual(task.state, "error")
            self.assertEqual(task.result["error_code"], "no_configuration_changes")

    def test_config_changed_after_preview_never_overwrites_external_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            current = "request-retry: 9\n"
            config.write_text(current, encoding="utf-8")
            task = SimpleNamespace(result={}, generation=server.STORE.apply_generation,
                                   set_stage=lambda stage: None)
            with patch.object(server, "write_local") as write, patch.object(server, "_run_apply_tail") as tail:
                server._commit_apply(task, {"base_raw": "request-retry: 1\n",
                                           "preview": "request-retry: 2\n"},
                                     {}, str(config), "", "", "")
            write.assert_not_called()
            tail.assert_not_called()
            self.assertEqual(task.result["error_code"], "stale_config")
            self.assertEqual(config.read_text(encoding="utf-8"), current)


if __name__ == "__main__":
    unittest.main()
