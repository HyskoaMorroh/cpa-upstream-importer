#!/usr/bin/env python3
"""Deployment contracts for the backup/ledger directory and policy honesty.

Runs offline. The container-level failure these cover was measured on
2026-10-04: the image chowns /backups to APP_UID, the deployed compose keeps
``cap_drop: [ALL]`` and defaults ``user`` to ``0:0``, so root has no
CAP_DAC_OVERRIDE and cannot create ``/backups/probe-policy.sqlite3`` on a fresh
named volume. The probe policy then fails closed and every section reports
"未实测 — 持久账本不可用" with zero upstream requests, while /api/context still
claims the sites are authorized.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class BackupDirectoryOwnershipTests(unittest.TestCase):
    """The ledger directory must be writable by every documented runtime uid."""

    def setUp(self):
        self.dockerfile = (ROOT / 'deploy/Dockerfile').read_text(encoding='utf-8')

    def test_backup_directory_is_group_writable_for_dropped_capabilities(self):
        # A chown alone is not enough: the deployed compose runs as uid 0 with
        # every capability dropped, which makes root an ordinary user that the
        # 0755 owner-only permission locks out of its own ledger directory.
        match = re.search(r'chmod\s+(?:-R\s+)?([0-7]{3,4})\s+[^\n]*?/backups', self.dockerfile)
        self.assertIsNotNone(
            match, 'deploy/Dockerfile must set explicit permissions on /backups')
        mode = int(match.group(1), 8)
        self.assertTrue(mode & 0o020,
                        f'/backups mode {oct(mode)} is not group writable')
        self.assertTrue(mode & 0o002,
                        f'/backups mode {oct(mode)} blocks a uid outside the image group')

    def test_the_configured_backup_directory_is_the_ledger_directory(self):
        compose = ROOT / 'docker-compose.yml'
        if not compose.is_file():
            self.skipTest('docker-compose.yml is not mounted in this test container')
        text = compose.read_text(encoding='utf-8')
        self.assertIn('IMPORTER_PROBE_LEDGER', text)
        self.assertRegex(text, r'IMPORTER_PROBE_LEDGER:\s*"\$\{IMPORTER_PROBE_LEDGER:-/backups/')


class PolicyHonestyTests(unittest.TestCase):
    """Authorization text must not contradict a ledger that denies every probe."""

    def test_summary_reports_a_blocked_ledger_instead_of_authorized_sites(self):
        from cpa_probe.probe_policy import ProbePolicy

        policy = {'version': 1, 'sites': {'api.fixture.invalid': {
            'enabled': True, 'provider': 'fixture', 'max_requests': 5,
            'max_requests_per_credential': 5, 'window_seconds': 60,
            'min_interval_seconds': 0}}}
        unusable = ProbePolicy(policy, str(ROOT / 'tests' / 'missing-dir' / 'ledger.sqlite3'))
        summary = unusable.summary()
        self.assertEqual(0, summary['authorized_sites'],
                         'a denied ledger must not be reported as authorized sites')
        self.assertEqual('passive', summary['mode'])
        self.assertIn('账本', summary['reason'])
        self.assertTrue(summary.get('ledger_blocked'))

    def test_a_working_ledger_still_reports_its_authorized_sites(self):
        import tempfile

        from cpa_probe.probe_policy import ProbePolicy

        policy = {'version': 1, 'sites': {'api.fixture.invalid': {
            'enabled': True, 'provider': 'fixture', 'max_requests': 5,
            'max_requests_per_credential': 5, 'window_seconds': 60,
            'min_interval_seconds': 0}}}
        with tempfile.TemporaryDirectory() as temp:
            summary = ProbePolicy(policy, os.path.join(temp, 'ledger.sqlite3')).summary()
        self.assertEqual(1, summary['authorized_sites'])
        self.assertEqual('restricted', summary['mode'])
        self.assertFalse(summary.get('ledger_blocked'))


if __name__ == '__main__':
    unittest.main()
