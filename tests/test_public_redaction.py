"""Public-response redaction must stay safe without parsing YAML per leaf."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class PublicRedactionTests(unittest.TestCase):
    def test_batch_output_does_not_serialize_yaml_for_every_metadata_string(self):
        payload = {"items": [{"status": "ok", "model": "fixture-model", "elapsed": i}
                             for i in range(300)]}
        with mock.patch.object(yaml, "safe_dump", wraps=yaml.safe_dump) as dump:
            result = server._public_with_context(payload, {})
        self.assertEqual(result, payload)
        self.assertLessEqual(dump.call_count, 4,
                             "one public response must not run a YAML round-trip per string")

    def test_known_credentials_are_removed_from_prose_and_typed_fields(self):
        secret = "fixture-secret-long-42"
        payload = {"message": f"request rejected: {secret}",
                   "codex-api-key": [{"api-key": secret, "model": "fixture-model"}]}
        before = copy.deepcopy(payload)
        result = server._public_with_context(payload, {"api-key": secret})
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(payload, before)
        self.assertIsInstance(result["codex-api-key"], list)

    def test_url_credentials_are_removed_but_host_remains_visible(self):
        url = "https://fixture-user:fixture-password@fixture.invalid/v1?api_key=fixture-query-key"
        result = server._public_with_context({"base_url": url}, {})["base_url"]
        self.assertIn("fixture.invalid", result)
        self.assertNotIn("fixture-password", result)
        self.assertNotIn("fixture-query-key", result)

    def test_header_secrets_are_still_structurally_redacted(self):
        payload = {"request_headers": {
            "Authorization": "Bearer fixture-auth-secret",
            "X-Api-Key": "fixture-header-secret",
            "User-Agent": "fixture-client",
        }}
        result = server._public_with_context(payload, {})
        self.assertNotIn("fixture-auth-secret", json.dumps(result))
        self.assertNotIn("fixture-header-secret", json.dumps(result))
        self.assertEqual(result["request_headers"]["User-Agent"], "fixture-client")

    def test_integrity_fields_and_non_string_types_survive_redaction(self):
        revision = "00112233445566778899aabbccddeeff" * 2
        payload = {"revision": revision, "fingerprint": revision,
                   "bulk_id": "fixture-bulk-id", "section": "codex-api-key",
                   "count": 0, "enabled": False, "unknown": None,
                   "items": [0, False, None]}
        context = {"headers": {"X-Private-Value": "0"}}
        result = server._public_with_context(payload, context)
        self.assertEqual(result, payload)

    def test_standalone_public_redaction_remains_safe(self):
        result = server._public({"message": "Authorization: Bearer fixture-secret"})
        self.assertNotIn("fixture-secret", result["message"])


if __name__ == "__main__":
    unittest.main()
