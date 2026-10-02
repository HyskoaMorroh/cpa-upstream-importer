#!/usr/bin/env python3
"""Offline policy tests: synthetic hosts, temporary ledgers, no sockets."""
from __future__ import annotations

import dataclasses
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = "https://api.fixture.invalid/v1/models"
HEADERS = {"Authorization": "Bearer synthetic-credential-one"}


def configuration(**overrides):
    rule = dict(enabled=True, provider="fixture-provider", max_requests=3,
                max_requests_per_credential=2, window_seconds=60,
                min_interval_seconds=0)
    rule.update(overrides)
    return {"version": 1, "sites": {"api.fixture.invalid": rule}}


def child_reserve(policy, ledger, output, ready=None):
    from cpa_probe.probe_policy import ProbePolicy
    if ready is not None:
        ready.wait(timeout=10)
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
            patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")):
        permit = ProbePolicy(policy, ledger).reserve(URL, HEADERS)
        output.put((permit.allowed, permit.code))


class ProbePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = str(self.root / "ledger.sqlite3")
        self.module = importlib.import_module("cpa_probe.probe_policy")
        self.addCleanup(self.module.reset_policy_cache)
        self.clock = patch.object(self.module.time, "time", return_value=1000.0).start()
        self.addCleanup(patch.stopall)
        self.connect = patch.object(socket.socket, "connect", side_effect=AssertionError("network")).start()
        self.dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")).start()
        self.opener = patch("urllib.request.urlopen", side_effect=AssertionError("network")).start()

    def engine(self, policy=None):
        return self.module.ProbePolicy(configuration() if policy is None else policy, self.ledger)

    def send(self, engine, headers=HEADERS, status="200", body="", error="", url=URL):
        permit = engine.reserve(url, headers)
        self.assertTrue(permit.allowed, permit.reason)
        engine.finish(permit, status, body, error)
        return permit

    def deny(self, decision, code=None):
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.code)
        self.assertTrue(decision.reason)
        if code:
            self.assertEqual(decision.code, code)

    def test_passive_defaults_never_create_ledger_or_open_network(self):
        for policy in ({}, {"version": 1, "sites": {}}, None, []):
            engine = self.module.ProbePolicy(policy, self.ledger)
            self.deny(engine.check(URL, HEADERS))
            self.deny(engine.reserve(URL, HEADERS))
            self.assertEqual(engine.summary()["mode"], "passive")
        self.assertFalse(Path(self.ledger).exists())
        self.connect.assert_not_called()
        self.dns.assert_not_called()
        self.opener.assert_not_called()

    def test_all_rule_fields_are_mandatory_and_strictly_typed(self):
        values = {
            "enabled": [None, 1, "true", False],
            "provider": [None, "", " ", 12, "*"],
            "max_requests": [None, True, 0, -1, 1.5, "2", 2 ** 64],
            "max_requests_per_credential": [None, True, 0, -1, 1.5, "2", 2 ** 64],
            "window_seconds": [None, True, 0, -1, "2", float("nan"), float("inf")],
            "min_interval_seconds": [None, True, -1, "2", float("nan"), float("inf")],
        }
        for field, invalid_values in values.items():
            missing = configuration()
            del missing["sites"]["api.fixture.invalid"][field]
            for policy in [missing] + [configuration(**{field: value}) for value in invalid_values]:
                with self.subTest(field=field, policy=policy):
                    self.deny(self.engine(policy).reserve(URL, HEADERS))
        self.assertFalse(Path(self.ledger).exists())

    def test_malformed_versions_hosts_and_conflicting_aliases_fail_closed(self):
        for version in (True, 0, 2, "1", None):
            cfg = configuration()
            cfg["version"] = version
            self.deny(self.engine(cfg).check(URL))
        for host in ("*", "*.fixture.invalid", "api.fixture.invalid/*", "https://api.fixture.invalid",
                     "api.fixture.invalid:443", "api.fixture.invalid?x", "api.fixture.invalid\n"):
            cfg = configuration()
            cfg["sites"] = {host: next(iter(cfg["sites"].values()))}
            self.deny(self.engine(cfg).reserve(URL))
        cfg = configuration()
        cfg["sites"]["alias.fixture.invalid"] = configuration(max_requests=4)["sites"]["api.fixture.invalid"]
        self.deny(self.engine(cfg).reserve(URL))
        self.assertFalse(Path(self.ledger).exists())

    def test_url_matching_is_exact_and_invalid_credentials_are_denied(self):
        engine = self.engine()
        for url in ("https://other.fixture.invalid", "https://api.fixture.invalid.evil.invalid",
                    "file:///api.fixture.invalid", "https://user:pass@api.fixture.invalid",
                    "https://api.fixture.invalid:bad", "https://api.fixture.invalid\n.evil.invalid"):
            self.deny(engine.reserve(url, HEADERS))
        for headers in ([], {"Authorization": 123}, {"Authorization": "Bearer "},
                        {"Authorization": "Bearer one", "x-api-key": "two"}):
            self.deny(engine.reserve(URL, headers))
        self.assertFalse(Path(self.ledger).exists())
        permit = engine.reserve("wss://API.FIXTURE.INVALID.:8443/realtime", HEADERS)
        self.assertTrue(permit.allowed)
        engine.finish(permit, "101")

    def test_check_spends_no_budget_and_never_reserves(self):
        engine = self.engine(configuration(max_requests=1, max_requests_per_credential=1))
        for _ in range(5):
            self.assertTrue(engine.check(URL, HEADERS).allowed)
        self.send(engine)
        self.deny(engine.check(URL, HEADERS), "provider_budget")
        self.deny(engine.reserve(URL, HEADERS), "provider_budget")

    def test_provider_budget_survives_new_keys_proxies_paths_aliases_and_instances(self):
        cfg = configuration(max_requests=2, max_requests_per_credential=1)
        cfg["sites"]["alias.fixture.invalid"] = dict(cfg["sites"]["api.fixture.invalid"])
        self.send(self.engine(cfg))
        self.send(self.engine(cfg), {"x-api-key": "synthetic-second", "Proxy-Authorization": "proxy-one"},
                  url="https://alias.fixture.invalid/another-section?task=second")
        self.deny(self.engine(cfg).reserve(URL + "?job=new", {
            "Authorization": "Bearer synthetic-third", "Proxy-Authorization": "proxy-two"
        }), "provider_budget")

    def test_credential_budget_ignores_header_case_and_noncredential_headers(self):
        cfg = configuration(max_requests=9, max_requests_per_credential=1)
        self.send(self.engine(cfg))
        for headers in ({"authorization": "bearer synthetic-credential-one", "User-Agent": "new"},
                        {"X-Api-Key": "synthetic-credential-one"},
                        {"X-Goog-Api-Key": "synthetic-credential-one"}):
            self.deny(self.engine(cfg).reserve(URL, headers), "credential_budget")
        self.deny(self.engine(cfg).reserve(URL + "?key=synthetic-credential-one", {}), "credential_budget")
        self.send(self.engine(cfg), {"Authorization": "Bearer synthetic-other"})

    def test_anonymous_requests_share_one_credential_bucket(self):
        cfg = configuration(max_requests=9, max_requests_per_credential=1)
        self.send(self.engine(cfg), {})
        self.deny(self.engine(cfg).reserve(URL + "?task=other", {"User-Agent": "other"}), "credential_budget")

    def test_minimum_interval_and_clock_rollback(self):
        engine = self.engine(configuration(min_interval_seconds=10))
        self.send(engine)
        self.clock.return_value = 1009.99
        self.deny(self.engine().reserve(URL, HEADERS), "minimum_interval")
        self.clock.return_value = 999.0
        self.deny(engine.check(URL, HEADERS), "clock_rollback")
        self.deny(engine.reserve(URL, HEADERS), "clock_rollback")
        self.clock.return_value = 1010.0
        self.send(engine)

    def test_window_boundary_and_rollback_after_window_advance(self):
        cfg = configuration(max_requests=1, max_requests_per_credential=1)
        self.send(self.engine(cfg))
        self.clock.return_value = 1059.999
        self.deny(self.engine(cfg).reserve(URL, HEADERS), "provider_budget")
        self.clock.return_value = 1060.0
        self.send(self.engine(cfg))
        self.clock.return_value = 1001.0
        self.deny(self.engine(cfg).reserve(URL, HEADERS), "clock_rollback")

    def test_shortened_policy_window_does_not_erase_recent_usage(self):
        self.send(self.engine(configuration(max_requests=1)))
        self.clock.return_value = 1002.0
        self.deny(self.engine(configuration(max_requests=1, window_seconds=1)).reserve(URL, HEADERS),
                  "provider_budget")

    def test_new_provider_name_for_same_site_cannot_reset_usage(self):
        self.send(self.engine(configuration(max_requests=1)))
        self.deny(self.engine(configuration(provider="new-name", max_requests=1)).reserve(URL, HEADERS),
                  "provider_changed")

    def test_unfinished_reservation_never_expires_or_gets_refunded(self):
        cfg = configuration(max_requests=9, max_requests_per_credential=9)
        engine = self.engine(cfg)
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed)
        self.clock.return_value = 100000.0
        self.deny(self.engine(cfg).reserve(URL, {"x-api-key": "other"}), "inflight")
        self.deny(engine.check(URL, HEADERS), "inflight")
        engine.finish(permit, "000", error="synthetic connection failure")
        self.send(engine)

    def test_only_original_issuing_instance_and_permit_can_finish(self):
        cfg = configuration(max_requests=2, max_requests_per_credential=2)
        engine = self.engine(cfg)
        permit = engine.reserve(URL, HEADERS)
        self.engine(cfg).finish(permit, "200")
        engine.finish(dataclasses.replace(permit), "200")
        engine.finish(self.module.Permit(False, "denied", "denied", permit.reservation_id), "200")
        self.deny(engine.reserve(URL, HEADERS), "inflight")
        engine.finish(permit, "200")
        engine.finish(permit, "403")  # Already consumed permits cannot alter another outcome.
        self.send(engine)
        self.deny(engine.reserve(URL, HEADERS), "provider_budget")

    def test_failures_consume_reservation_without_refund(self):
        cfg = configuration(max_requests=1, max_requests_per_credential=1)
        self.send(self.engine(cfg), status="000", error="synthetic timeout")
        self.deny(self.engine(cfg).reserve(URL, HEADERS), "provider_budget")

    def test_restrictions_persist_across_instances_windows_and_policy_changes(self):
        outcomes = [("403", "", ""), ("429", "", ""),
                    ("200", "Automated probing is prohibited", ""),
                    ("200", "Your account has been suspended", ""),
                    ("200", "该站禁止探测，账号已封禁", ""),
                    ("000", "", "API key has been banned")]
        for index, (status, body, error) in enumerate(outcomes):
            with self.subTest(status=status, body=body):
                self.ledger = str(self.root / (str(index) + ".sqlite3"))
                self.clock.return_value = 1000.0
                self.send(self.engine(), status=status, body=body, error=error)
                self.clock.return_value = 100000.0
                cfg = configuration(max_requests=999, max_requests_per_credential=999)
                self.deny(self.engine(cfg).reserve(URL, {"x-api-key": "other"}), "provider_stopped")

    def test_client_restrictions_stop_without_403_or_429(self):
        outcomes = [("401", "only official clients are allowed"),
                    ("503", "This group is restricted to Claude Code"),
                    ("400", "automated tests prohibited")]
        for index, (status, body) in enumerate(outcomes):
            with self.subTest(status=status):
                self.ledger = str(self.root / ("restriction-extra-" + str(index) + ".sqlite3"))
                self.send(self.engine(), status=status, body=body)
                self.deny(self.engine().check(URL, {"x-api-key": "different-key"}), "provider_stopped")

    def test_finish_reports_a_failed_ledger_write(self):
        engine = self.engine()
        permit = engine.reserve(URL, HEADERS)
        with patch.object(engine, "_connect", side_effect=sqlite3.OperationalError("synthetic")):
            self.assertIs(engine.finish(permit, "200"), False)
        self.deny(engine.check(URL, HEADERS))

    def test_general_errors_do_not_pretend_to_be_provider_bans(self):
        for body in ("invalid model", "banned_model is not a known model", '{"banned": false}', "temporary timeout"):
            self.send(self.engine(configuration(max_requests=20, max_requests_per_credential=20)), body=body)

    def test_corrupt_empty_directory_and_unwritable_ledgers_fail_closed(self):
        for content in (b"not a sqlite database", b""):
            Path(self.ledger).write_bytes(content)
            engine = self.engine()
            self.deny(engine.check(URL, HEADERS), "ledger_unavailable")
            self.deny(engine.reserve(URL, HEADERS), "ledger_unavailable")
            self.assertEqual(Path(self.ledger).read_bytes(), content)
        self.ledger = str(self.root)
        self.deny(self.engine().reserve(URL, HEADERS), "ledger_unavailable")
        self.ledger = str(self.root / "missing-parent" / "ledger.sqlite3")
        self.deny(self.engine().reserve(URL, HEADERS), "ledger_unavailable")
        self.ledger = str(self.root / "unwritable.sqlite3")
        with patch.object(self.module.sqlite3, "connect", side_effect=sqlite3.OperationalError("private path")):
            self.deny(self.engine().reserve(URL, HEADERS), "ledger_unavailable")

    def test_readonly_existing_ledger_and_write_failure_during_finish_deny(self):
        engine = self.engine()
        permit = engine.reserve(URL, HEADERS)
        real_connect = sqlite3.connect

        def readonly(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.execute("PRAGMA query_only=ON")
            return connection

        with patch.object(self.module.sqlite3, "connect", side_effect=readonly):
            self.deny(self.engine().check(URL, HEADERS), "ledger_unavailable")
            self.deny(self.engine().reserve(URL, HEADERS), "ledger_unavailable")
            engine.finish(permit, "403")
        # No write must leave the outstanding reservation blocking every new sender.
        self.deny(self.engine().reserve(URL, HEADERS), "inflight")
        engine.finish(permit, "403")
        self.deny(self.engine().reserve(URL, HEADERS), "provider_stopped")

    def test_sqlite_lock_returns_deny_without_retry_loop(self):
        engine = self.engine()
        self.assertTrue(engine.check(URL, HEADERS).allowed)
        with closing(sqlite3.connect(self.ledger)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            self.deny(engine.reserve(URL, HEADERS), "ledger_unavailable")

    def test_ledger_and_diagnostics_do_not_contain_credentials_headers_or_urls(self):
        headers = {"Authorization": "Bearer synthetic-private-credential", "X-Private": "private-header"}
        engine = self.engine()
        url = URL + "?secret=private-query"
        self.send(engine, headers, status="403", body="private response", error="private error", url=url)
        diagnostic = repr(engine.summary()) + repr(engine.reserve(url, headers))
        with closing(sqlite3.connect(self.ledger)) as db:
            diagnostic += "\n".join(db.iterdump())
        diagnostic += Path(self.ledger).read_bytes().decode("latin1")
        for secret in ("synthetic-private-credential", "Bearer", "private-header", url,
                       "private-query", "private response", "private error"):
            self.assertNotIn(secret, diagnostic)

    def test_multithread_instances_allow_only_one_inflight(self):
        self.assertTrue(self.engine().check(URL, HEADERS).allowed)
        barrier = threading.Barrier(12)

        def reserve(index):
            engine = self.engine(configuration(max_requests=99, max_requests_per_credential=99))
            barrier.wait(timeout=10)
            return engine, engine.reserve(URL, {"x-api-key": "synthetic-" + str(index)})

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(12)))
        allowed = [(engine, permit) for engine, permit in results if permit.allowed]
        self.assertEqual(len(allowed), 1)
        allowed[0][0].finish(allowed[0][1], "200")

    def test_concurrent_instances_cannot_exceed_provider_or_credential_counts(self):
        for same_key, maximum, credential_max in ((False, 4, 2), (True, 20, 2)):
            self.ledger = str(self.root / ("same.sqlite3" if same_key else "different.sqlite3"))
            cfg = configuration(max_requests=maximum, max_requests_per_credential=credential_max)
            self.assertTrue(self.engine(cfg).check(URL, HEADERS).allowed)

            def attempts(index):
                engine = self.engine(cfg)
                headers = HEADERS if same_key else {"x-api-key": "synthetic-" + str(index)}
                sent = 0
                for _ in range(12):
                    permit = engine.reserve(URL, headers)
                    if permit.allowed:
                        sent += 1
                        engine.finish(permit, "200")
                return sent

            with ThreadPoolExecutor(max_workers=8) as pool:
                sent = sum(pool.map(attempts, range(8)))
            self.assertEqual(sent, credential_max if same_key else maximum)

    def test_new_process_observes_usage_stop_and_unfinished_reservation(self):
        ctx = multiprocessing.get_context("spawn")
        cfg = configuration(max_requests=1, max_requests_per_credential=1)
        for state in ("usage", "stop", "inflight"):
            self.ledger = str(self.root / (state + ".sqlite3"))
            engine = self.engine(cfg)
            permit = engine.reserve(URL, HEADERS)
            self.assertTrue(permit.allowed)
            if state != "inflight":
                engine.finish(permit, "403" if state == "stop" else "200")
            # Child uses real wall clock. A long window keeps the synthetic old usage in scope.
            child_cfg = configuration(max_requests=1, max_requests_per_credential=1, window_seconds=10 ** 12)
            output = ctx.Queue()
            process = ctx.Process(target=child_reserve, args=(child_cfg, self.ledger, output))
            process.start()
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
            allowed, code = output.get(timeout=3)
            self.assertFalse(allowed)
            self.assertEqual(code, {"usage": "provider_budget", "stop": "provider_stopped", "inflight": "inflight"}[state])
            output.close()
            output.join_thread()

    def test_environment_cache_policy_removal_recreation_and_redacted_summary(self):
        policy_path = self.root / "policy.json"
        env = {"IMPORTER_PROBE_POLICY_FILE": str(policy_path), "IMPORTER_PROBE_LEDGER": "",
               "IMPORTER_BACKUP_DIR": str(self.root)}
        with patch.dict(os.environ, env):
            self.module.reset_policy_cache()
            self.deny(self.module.get_policy().reserve(URL, HEADERS))
            self.assertFalse((self.root / "probe-policy.sqlite3").exists())
            policy_path.write_text(json.dumps(configuration(max_requests=1)), encoding="utf-8")
            self.send(self.module.get_policy())
            self.assertTrue((self.root / "probe-policy.sqlite3").exists())
            policy_path.unlink()
            self.deny(self.module.get_policy().reserve(URL, HEADERS))
            policy_path.write_text("{bad json", encoding="utf-8")
            self.deny(self.module.get_policy().reserve(URL, HEADERS))
            policy_path.write_text(json.dumps(configuration(max_requests=1)), encoding="utf-8")
            self.module.reset_policy_cache()
            self.deny(self.module.get_policy().reserve(URL, HEADERS), "provider_budget")
            summary = self.module.get_policy().summary()
            self.assertTrue(summary["default_passive"])
            self.assertEqual(summary["enabled_sites"], 1)
            self.assertNotIn(str(self.root), repr(summary))

    def test_failed_stop_write_cannot_later_be_overwritten_by_success(self):
        engine = self.engine()
        permit = engine.reserve(URL, HEADERS)
        with patch.object(self.module.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk full")):
            engine.finish(permit, "429")
        engine.finish(permit, "200")
        self.deny(self.engine().reserve(URL, HEADERS), "provider_stopped")

    def test_duplicate_json_fields_are_malformed_not_last_value_wins(self):
        policy_path = self.root / "policy.json"
        content = json.dumps(configuration()).replace('"enabled": true', '"enabled": false, "enabled": true')
        policy_path.write_text(content, encoding="utf-8")
        with patch.dict(os.environ, {"IMPORTER_PROBE_POLICY_FILE": str(policy_path),
                                    "IMPORTER_PROBE_LEDGER": self.ledger}):
            self.module.reset_policy_cache()
            self.deny(self.module.get_policy().reserve(URL, HEADERS))
        self.assertFalse(Path(self.ledger).exists())

    def test_multiple_processes_cannot_reserve_concurrently(self):
        cfg = configuration(window_seconds=10 ** 12)
        self.assertTrue(self.engine(cfg).check(URL, HEADERS).allowed)
        ctx = multiprocessing.get_context("spawn")
        ready, output = ctx.Event(), ctx.Queue()
        processes = [ctx.Process(target=child_reserve, args=(cfg, self.ledger, output, ready)) for _ in range(6)]
        for process in processes:
            process.start()
        ready.set()
        for process in processes:
            process.join(timeout=15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        results = [output.get(timeout=3) for _ in processes]
        output.close()
        output.join_thread()
        self.assertEqual(sum(allowed for allowed, _ in results), 1)

    def test_stops_and_minimum_interval_are_visible_to_check(self):
        engine = self.engine(configuration(min_interval_seconds=10))
        self.send(engine)
        self.deny(engine.check(URL, HEADERS), "minimum_interval")
        self.clock.return_value = 1010.0
        self.send(engine, status="403")
        self.deny(engine.check(URL, HEADERS), "provider_stopped")

    def test_cache_reload_keeps_permit_owner_while_rules_are_removed(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(configuration()), encoding="utf-8")
        with patch.dict(os.environ, {"IMPORTER_PROBE_POLICY_FILE": str(policy_path),
                                    "IMPORTER_PROBE_LEDGER": self.ledger}):
            self.module.reset_policy_cache()
            engine = self.module.get_policy()
            permit = engine.reserve(URL, HEADERS)
            policy_path.unlink()
            reloaded = self.module.get_policy()
            self.assertIs(reloaded, engine)
            reloaded.finish(permit, "403")
            policy_path.write_text(json.dumps(configuration()), encoding="utf-8")
            self.deny(self.module.get_policy().check(URL, HEADERS), "provider_stopped")

    def test_environment_explicit_ledger_overrides_backup_dir(self):
        policy_path = self.root / "policy.json"
        policy_path.write_text(json.dumps(configuration()), encoding="utf-8")
        with patch.dict(os.environ, {"IMPORTER_PROBE_POLICY_FILE": str(policy_path),
                                    "IMPORTER_PROBE_LEDGER": self.ledger,
                                    "IMPORTER_BACKUP_DIR": str(self.root / "unused")}):
            self.module.reset_policy_cache()
            self.send(self.module.get_policy())
        self.assertTrue(Path(self.ledger).exists())
        self.assertFalse((self.root / "unused").exists())


if __name__ == "__main__":
    unittest.main()
