"""Publishing requires every test runtime; missing tools are failures."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("release_runner", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

class ReleaseGateTests(unittest.TestCase):
    def test_missing_node_fails(self):
        with patch.object(runner.subprocess, "run", side_effect=FileNotFoundError("node")):
            passed, failed, reason = runner._run_node_suites()
        self.assertEqual(0, passed)
        self.assertTrue(failed, "a missing runtime must make the release gate fail")

    def test_no_executed_node_cases_fails(self):
        version = subprocess.CompletedProcess([], 0, "v22.0.0", "")
        empty = subprocess.CompletedProcess([], 0, chr(10).join(["# tests 0", "# pass 0", "# fail 0"]), "")
        with patch.object(runner.subprocess, "run", side_effect=[version, empty]):
            passed, failed, reason = runner._run_node_suites()
        self.assertTrue(failed)

    def test_node_timeout_fails_without_leaking_exception(self):
        version = subprocess.CompletedProcess([], 0, "v22.0.0", "")
        with patch.object(runner.subprocess, "run", side_effect=[version, subprocess.TimeoutExpired("node", 600)]):
            passed, failed, reason = runner._run_node_suites()
        self.assertTrue(failed)

    def test_tap_counts_are_read(self):
        version = subprocess.CompletedProcess([], 0, "v22.0.0", "")
        result = subprocess.CompletedProcess([], 0, chr(10).join(["# tests 33", "# pass 33", "# fail 0"]), "")
        with patch.object(runner.subprocess, "run", side_effect=[version, result]):
            passed, failed, reason = runner._run_node_suites()
        self.assertEqual((33, [], ""), (passed, failed, reason))

if __name__ == "__main__":
    unittest.main()
