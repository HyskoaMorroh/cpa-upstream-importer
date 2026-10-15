"""Authenticated settlement recovery; synthetic inputs and no network."""
import dataclasses
import gc
import hashlib
import hmac
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch
import unittest

import test_probe_policy as fixtures

URL, HEADERS, configuration = fixtures.URL, fixtures.HEADERS, fixtures.configuration


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class SettlementRecoveryTests(unittest.TestCase):
    setUp = fixtures.ProbePolicyTests.setUp
    engine = fixtures.ProbePolicyTests.engine
    deny = fixtures.ProbePolicyTests.deny

    def deferred(self, engine, status="200", body="", retry_after=""):
        permit = engine.reserve(URL, HEADERS)
        self.assertTrue(permit.allowed)
        with closing(sqlite3.connect(self.ledger)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            self.assertFalse(engine.finish(permit, status, body, retry_after=retry_after))
        return permit

    def rewrite_evidence(self, engine, transform, sign=True):
        with database(self.ledger + ".settlements.sqlite3") as side:
            row = side.execute("SELECT reservation,payload,signature FROM settlements").fetchone()
            payload = json.dumps(transform(json.loads(row[1])), sort_keys=True, separators=(",", ":"))
            signature = hmac.new(engine._ledger_identity, payload.encode(), hashlib.sha256).hexdigest() if sign else row[2]
            side.execute("UPDATE settlements SET payload=?,signature=? WHERE reservation=?", (payload, signature, row[0]))

    def test_restart_replays_restriction_once_without_new_reservation(self):
        owner = self.engine()
        permit = self.deferred(owner, "403", "account suspended")
        del owner
        gc.collect()
        fresh = self.engine()
        self.deny(fresh.check(URL, HEADERS), "provider_stopped")
        self.deny(fresh.check(URL, HEADERS), "provider_stopped")
        with database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM restriction_evidence").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT outcome FROM reservations WHERE id=?", (permit.reservation_id,)).fetchone()[0], "restriction")
        self.assertEqual(fresh.summary()["reservation_recovery_status"], "clear")

    def test_retry_after_expiry_uses_received_time_not_replay_time(self):
        owner = self.engine()
        self.deferred(owner, "429", retry_after="1800")
        self.clock.return_value = 2000.0
        fresh = self.engine()
        self.deny(fresh.check(URL, HEADERS), "provider_cooldown")
        with database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT until FROM cooldowns").fetchone()[0], 2800.0)
        self.clock.return_value = 2801.0
        self.assertTrue(self.engine().check(URL, HEADERS).allowed)

    def test_no_evidence_retired_owner_remains_unresolved_without_ttl(self):
        owner = self.engine()
        permit = owner.reserve(URL, HEADERS)
        sibling = self.engine()
        self.deny(sibling.check(URL, HEADERS), "inflight")
        self.assertEqual(sibling.summary()["reservation_recovery_status"], "active")
        del owner
        gc.collect()
        self.clock.return_value = 10**12
        self.deny(sibling.check(URL, HEADERS), "orphaned_inflight")
        summary = sibling.summary()
        self.assertEqual(summary["inflight_count"], 0)
        self.assertEqual(summary["orphaned_inflight_count"], 1)
        self.assertEqual(summary["reservation_recovery_status"], "unresolved")
        with database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT outcome FROM reservations WHERE id=?", (permit.reservation_id,)).fetchone()[0], "")

    def test_completed_original_is_idempotent_copied_permit_is_rejected(self):
        owner = self.engine()
        permit = owner.reserve(URL, HEADERS)
        self.assertFalse(owner.finish(dataclasses.replace(permit), "200"))
        self.assertTrue(owner.finish(permit, "403", "account suspended"))
        self.assertTrue(owner.finish(permit, "200"))
        self.assertFalse(owner.finish(dataclasses.replace(permit), "200"))
        self.assertFalse(self.engine().finish(permit, "200"))
        self.deny(self.engine().check(URL, HEADERS), "provider_stopped")

    def test_tampered_and_malformed_signed_results_fail_closed(self):
        mutations = (
            (lambda p: dict(p, stop="restriction"), False),
            (lambda p: [], True),
            (lambda p: {"reservation": p["reservation"]}, True),
            (lambda p: dict(p, provider="other"), True),
            (lambda p: dict(p, owner_epoch="other"), True),
            (lambda p: dict(p, scope="other"), True),
            (lambda p: dict(p, now="1000"), True),
        )
        for index, (transform, sign) in enumerate(mutations):
            with self.subTest(index=index):
                self.ledger = str(self.root / ("malformed-%s.sqlite3" % index))
                owner = self.engine()
                self.deferred(owner)
                self.rewrite_evidence(owner, transform, sign)
                self.deny(self.engine().reserve(URL, HEADERS), "ledger_unavailable")
                with database(self.ledger) as db:
                    self.assertEqual(db.execute("SELECT outcome FROM reservations").fetchone()[0], "")
                    self.assertNotEqual(db.execute("SELECT inflight FROM providers").fetchone()[0], "")

    def test_cross_ledger_replay_even_same_reservation_rejected(self):
        owner = self.engine()
        first = self.deferred(owner)
        with database(self.ledger + ".settlements.sqlite3") as side:
            evidence = side.execute("SELECT * FROM settlements").fetchone()
        self.ledger = str(self.root / "other.sqlite3")
        other = self.engine()
        second = other.reserve(URL, HEADERS)
        with database(self.ledger) as db:
            db.execute("UPDATE reservations SET id=? WHERE id=?", (first.reservation_id, second.reservation_id))
            db.execute("UPDATE providers SET inflight=?", (first.reservation_id,))
            db.execute("UPDATE reservation_owners SET reservation=?", (first.reservation_id,))
        with database(self.ledger + ".settlements.sqlite3") as side:
            side.execute("INSERT INTO settlements VALUES (?,?,?)", evidence)
        self.deny(self.engine().check(URL, HEADERS), "ledger_unavailable")

    def test_failed_sidecar_write_does_not_release_after_restart(self):
        owner = self.engine()
        permit = owner.reserve(URL, HEADERS)
        with patch.object(owner, "_persist_result", side_effect=OSError("synthetic")):
            self.assertFalse(owner.finish(permit, "200"))
        del owner
        gc.collect()
        self.deny(self.engine().check(URL, HEADERS), "orphaned_inflight")

    def test_concurrent_replay_and_finish_first_result_wins(self):
        owner = self.engine(configuration(max_requests=1, max_requests_per_credential=1))
        permit = self.deferred(owner, "403", "account suspended")
        def race(index):
            if index % 2:
                return owner.finish(permit, "200")
            return self.engine().check(URL, HEADERS).code
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(race, range(24)))
        self.assertTrue(owner.finish(permit, "200"))
        self.deny(self.engine().check(URL, HEADERS), "provider_stopped")
        with database(self.ledger) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM restriction_evidence").fetchone()[0], 1)
        side = Path(self.ledger + ".settlements.sqlite3")
        self.assertEqual(side.parent, Path(self.ledger).parent)
        if os.name != "nt":
            self.assertEqual(side.stat().st_mode & 0o777, 0o600)
        data = side.read_bytes()
        for secret in (URL, HEADERS["Authorization"], "account suspended"):
            self.assertNotIn(secret.encode(), data)


if __name__ == "__main__":
    unittest.main()
