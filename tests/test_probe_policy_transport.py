#!/usr/bin/env python3
"""Policy/transport boundary tests: explicit fake engine, mock or loopback I/O only."""
from __future__ import annotations

import gzip
import io
import os
import sys
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cpa_probe import client
from test_transport_compliance import local_server, reply


class ControlledPolicy:
    """Deliberately installed transport double, not production authorization."""

    def __init__(self, allowance=0):
        self.allowance = allowance
        self.reservations = []
        self.finished = []
        self.active = set()
        self.stopped = False

    def reserve(self, url, headers=None):
        self.reservations.append((url, headers))
        code = "provider_stopped" if self.stopped else "budget_exhausted"
        allowed = not self.stopped and self.allowance > 0
        permit = SimpleNamespace(allowed=allowed, code="" if allowed else code,
                                 reason="" if allowed else "policy_skip: " + code,
                                 reservation_id=str(len(self.reservations)))
        if allowed:
            self.allowance -= 1
            self.active.add(permit.reservation_id)
        return permit

    def finish(self, permit, status, body="", error=""):
        self.active.remove(permit.reservation_id)
        self.finished.append((permit, status, body, error))
        if status in ("403", "429") or "automated probing forbidden" in body:
            self.stopped = True


def response(status=200, body=b'{"ok":true}', headers=None):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.status = status
    resp.headers = headers or {}
    resp.read.return_value = body
    resp.length = None
    return resp


class PolicyTransportTests(unittest.TestCase):
    def setUp(self):
        self.policy = ControlledPolicy()
        self.policy_patch = patch.object(client, "get_policy", return_value=self.policy,
                                         create=True)
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        self.opener_patch = patch.object(client, "_opener")
        self.opener = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.open = self.opener.return_value.open
        self.open.return_value = response()
        self.socket_patch = patch.object(client.socket, "create_connection")
        self.socket = self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.socket.return_value.recv.return_value = b"HTTP/1.1 400 Bad Request\r\n\r\nno"

    def fetch(self, **kwargs):
        return client.send(kwargs.pop("url", "https://fixture.invalid/v1/models"),
                           headers=kwargs.pop("headers", {}), body=b"",
                           method=kwargs.pop("method", "GET"), **kwargs)

    def real_engine(self):
        import tempfile
        from cpa_probe.probe_policy import ProbePolicy
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        rule = {"enabled": True, "provider": "fixture", "max_requests": 5,
                "max_requests_per_credential": 5, "window_seconds": 3600,
                "min_interval_seconds": 0}
        engine = ProbePolicy({"version": 1, "sites": {"fixture.invalid": rule}},
                             os.path.join(temp.name, "ledger.sqlite3"))
        client.get_policy.return_value = engine
        return engine

    def test_terminal_restriction_is_exposed_on_that_response(self):
        engine = self.real_engine()
        self.open.return_value = response(status=403, body=b"restricted")
        result = self.fetch()
        self.assertEqual("provider_stopped", result.policy_code)
        self.assertFalse(engine.check("https://fixture.invalid/v1/models", {}).allowed)

    def test_failed_settlement_cannot_report_a_clean_success(self):
        engine = self.real_engine()
        original = engine._connect
        count = [0]
        def connect():
            count[0] += 1
            if count[0] == 2:
                raise OSError("synthetic ledger failure")
            return original()
        with patch.object(engine, "_connect", side_effect=connect):
            result = self.fetch()
        self.assertEqual("policy_error", result.policy_code)
        self.assertNotEqual("200", result.status)

    def test_ws_restriction_in_separate_body_packet_stops_provider(self):
        engine = self.real_engine()
        body = b"automated tests prohibited"
        crlf = bytes([13, 10])
        head = b"HTTP/1.1 400 Bad Request" + crlf + b"Content-Length: " + str(len(body)).encode() + crlf + crlf
        self.socket.return_value.recv.side_effect = [head, body]
        result = client.ws_handshake("ws://fixture.invalid/socket", timeout=1)
        self.assertEqual("provider_stopped", result.policy_code)
        self.assertEqual(body.decode(), result.body)
        self.assertFalse(engine.check("https://fixture.invalid/v1/models", {}).allowed)

    def test_response_preserves_four_positional_arguments(self):
        result = client.Response("200", "ok", 1, "")
        self.assertEqual(result.policy_code, "")
        self.assertEqual(client.Response("000", "", 0, "skip",
                                         policy_code="denied").policy_code, "denied")

    def test_denied_http_never_builds_opener_or_socket_even_on_localhost(self):
        for url in ("https://fixture.invalid/", "http://localhost/", "http://127.0.0.1/"):
            with self.subTest(url=url):
                result = self.fetch(url=url, proxy="http://127.0.0.1:8080")
                self.assertEqual(result.status, "000")
                self.assertEqual(result.policy_code, "budget_exhausted")
                self.assertIn("policy_skip", result.error)
        self.opener.assert_not_called()
        self.socket.assert_not_called()
        self.assertEqual(self.policy.finished, [])

    def test_allowed_http_finishes_with_decoded_body_and_original_headers(self):
        self.policy.allowance = 1
        headers = {"X-Fixture-Account": "account-a"}
        self.open.return_value = response(body=gzip.compress(b"automated probing forbidden"))
        result = self.fetch(headers=headers)
        self.assertEqual(result.status, "200")
        self.assertEqual(self.policy.reservations, [("https://fixture.invalid/v1/models", headers)])
        self.assertEqual(self.policy.finished[0][1:], ("200", result.body, ""))
        self.assertEqual(self.policy.active, set())
        self.assertTrue(self.policy.stopped)

    def test_every_safe_retry_reserves_and_finishes_before_retry(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                self.policy = ControlledPolicy(2)
                client.get_policy.return_value = self.policy
                self.open.reset_mock()
                def attempt(*args, **kwargs):
                    self.assertEqual(len(self.policy.active), 1)
                    if not self.policy.finished:
                        raise ConnectionResetError("fixture diagnostic")
                    return response()
                self.open.side_effect = attempt
                result = self.fetch(method=method)
                self.assertEqual(result.status, "200")
                self.assertEqual(len(self.policy.reservations), 2)
                self.assertEqual(len(self.policy.finished), 2)
                self.assertEqual(self.open.call_count, 2)
                self.assertEqual(self.policy.active, set())

    def test_retry_exhaustion_prevents_second_open(self):
        self.policy.allowance = 1
        self.open.side_effect = urllib.error.URLError(ConnectionResetError("fixture"))
        result = self.fetch()
        self.assertEqual(result.policy_code, "budget_exhausted")
        self.assertEqual(len(self.policy.reservations), 2)
        self.assertEqual(len(self.policy.finished), 1)
        self.open.assert_called_once()
        self.assertEqual(self.policy.active, set())

    def test_post_never_replays_after_ambiguous_disconnect(self):
        self.policy.allowance = 2
        self.open.side_effect = ConnectionResetError("fixture")
        result = self.fetch(method="POST")
        self.assertEqual(result.status, "000")
        self.open.assert_called_once()
        self.assertEqual(len(self.policy.reservations), 1)
        self.assertEqual(len(self.policy.finished), 1)
        self.assertEqual(self.policy.active, set())

    def test_http_headers_body_and_decode_errors_always_release_permit(self):
        for where in ("headers", "body", "decode"):
            with self.subTest(where=where):
                self.policy = ControlledPolicy(2)
                client.get_policy.return_value = self.policy
                resp = response()
                if where == "headers":
                    resp.headers = MagicMock()
                    resp.headers.get.side_effect = ConnectionResetError("fixture marker")
                elif where == "body":
                    resp.read.side_effect = ConnectionResetError("fixture marker")
                else:
                    resp.read.return_value = b"\x80"
                self.open.reset_mock()
                self.open.return_value = resp
                result = self.fetch()
                self.assertEqual(result.status, "000")
                self.assertTrue(result.error)
                self.open.assert_called_once()
                self.assertEqual(len(self.policy.finished), 1)
                self.assertEqual(self.policy.finished[0][1], "200")
                self.assertEqual(self.policy.active, set())

    def test_http_error_read_failures_keep_stop_status_and_release(self):
        for status in (403, 429):
            for where in ("headers", "body"):
                with self.subTest(status=status, where=where):
                    self.policy = ControlledPolicy(2)
                    client.get_policy.return_value = self.policy
                    bad_headers = MagicMock()
                    bad_headers.get.side_effect = ValueError("fixture marker")
                    class BrokenBody(io.BytesIO):
                        def read(self, *args):
                            raise TimeoutError("fixture marker")
                    stream = BrokenBody()
                    error = urllib.error.HTTPError("https://fixture.invalid/", status,
                                                   "fixture", bad_headers if where == "headers" else {},
                                                   stream)
                    self.open.reset_mock()
                    self.open.side_effect = error
                    result = self.fetch()
                    self.assertEqual(result.status, str(status))
                    self.assertTrue(result.error)
                    self.assertEqual(self.policy.finished[0][1], str(status))
                    self.assertEqual(self.policy.active, set())
                    denied = self.fetch(headers={"X-Fixture-Account": "account-b"})
                    self.assertEqual(denied.policy_code, "provider_stopped")
                    self.open.assert_called_once()

    def test_untrusted_exception_messages_do_not_leak_into_errors_or_ledger(self):
        marker = "fixture-private-header-value"
        for exc in (ValueError(marker), urllib.error.URLError(marker),
                    ConnectionRefusedError(marker)):
            with self.subTest(kind=type(exc).__name__):
                self.policy.allowance = 1
                self.open.side_effect = exc
                result = self.fetch(headers={"X-Fixture-Account": marker})
                self.assertNotIn(marker, result.error)
                self.assertNotIn(marker, self.policy.finished[-1][3])

    def test_engine_failures_are_fail_closed_and_sanitized(self):
        with patch.object(self.policy, "reserve", side_effect=RuntimeError("fixture-private")):
            result = self.fetch()
        self.assertEqual(result.status, "000")
        self.assertTrue(result.policy_code)
        self.assertNotIn("fixture-private", result.error)
        self.opener.assert_not_called()
        self.socket.assert_not_called()

    def test_finish_failure_is_not_reported_as_http_success(self):
        self.policy.allowance = 1
        with patch.object(self.policy, "finish", side_effect=RuntimeError("fixture-private")):
            result = self.fetch()
        self.assertEqual(result.status, "000")
        self.assertTrue(result.policy_code)
        self.assertNotIn("fixture-private", result.error)
        self.open.assert_called_once()

    def test_denied_ws_never_opens_socket_and_exposes_policy_skip(self):
        for url in ("wss://fixture.invalid/socket", "ws://localhost/socket"):
            result = client.ws_handshake(url)
            self.assertEqual(result.status, "000")
            self.assertEqual(result.policy_code, "budget_exhausted")
            self.assertIn("policy_skip", result.error)
        self.socket.assert_not_called()
        self.assertEqual(self.policy.finished, [])

    def test_ws_finishes_with_original_body_contract_and_stop_status(self):
        self.policy.allowance = 2
        self.socket.return_value.recv.side_effect = [b"HTTP/1.1 429 Too Many Requests\r\n\r\nwait", b""]
        result = client.ws_handshake("ws://fixture.invalid/socket")
        self.assertEqual((result.status, result.body), ("429", "wait"))
        self.assertEqual(self.policy.finished[0][1:], ("429", "wait", ""))
        self.assertEqual(self.policy.active, set())
        denied = client.ws_handshake("ws://fixture.invalid/socket",
                                     headers={"X-Fixture-Account": "account-b"})
        self.assertEqual(denied.policy_code, "provider_stopped")
        self.socket.assert_called_once()

    def test_ws_partial_stop_headers_and_timeout_release_permit(self):
        self.policy.allowance = 1
        self.socket.return_value.recv.side_effect = [b"HTTP/1.1 403 Forbidden\r\n", TimeoutError()]
        result = client.ws_handshake("ws://fixture.invalid/socket")
        self.assertEqual(result.status, "000")
        self.assertIn("403", result.body)
        self.assertEqual(self.policy.finished[0][1], "403")
        self.assertEqual(self.policy.active, set())

    def test_ws_socket_and_tls_errors_release_without_exposing_messages(self):
        for tls in (False, True):
            with self.subTest(tls=tls):
                self.policy.allowance = 1
                with patch.object(client.ssl, "create_default_context") as context:
                    if tls:
                        self.socket.side_effect = None
                        context.return_value.wrap_socket.side_effect = ValueError("fixture-private")
                    else:
                        self.socket.side_effect = OSError("fixture-private")
                    result = client.ws_handshake("wss://fixture.invalid/socket")
                self.assertEqual(result.status, "000")
                self.assertNotIn("fixture-private", result.error)
                self.assertNotIn("fixture-private", self.policy.finished[-1][3])
                self.assertEqual(self.policy.active, set())

    def test_proxy_diagnostics_do_not_echo_credentials_or_exception_text(self):
        marker = "fixture-private"
        self.socket.side_effect = OSError(marker)
        ok, why = client.probe_proxy("http://127.0.0.1:12345")
        self.assertFalse(ok)
        self.assertNotIn(marker, why)
        ok, why = client.probe_proxy("http://" + marker + "@")
        self.assertFalse(ok)
        self.assertNotIn(marker, why)

    def test_ws_reset_after_partial_forbidden_headers_still_stops_engine(self):
        self.policy.allowance = 2
        self.socket.return_value.recv.side_effect = [b"HTTP/1.1 403 Forbidden\r\n",
                                                     ConnectionResetError("fixture-private")]
        result = client.ws_handshake("ws://fixture.invalid/socket")
        self.assertEqual(result.status, "000")
        self.assertNotIn("fixture-private", result.error)
        self.assertEqual(self.policy.finished[0][1], "403")
        self.assertTrue(self.policy.stopped)
        self.assertEqual(self.policy.active, set())

    def test_timeout_and_refused_connection_never_retry(self):
        for error in (TimeoutError(), ConnectionRefusedError()):
            with self.subTest(error=type(error).__name__):
                self.policy = ControlledPolicy(2)
                client.get_policy.return_value = self.policy
                self.open.reset_mock()
                self.open.side_effect = error
                result = self.fetch()
                self.assertEqual(result.status, "000")
                self.open.assert_called_once()
                self.assertEqual(len(self.policy.finished), 1)
                self.assertEqual(self.policy.active, set())

    def test_opener_construction_failure_releases_permit(self):
        self.policy.allowance = 1
        self.opener.side_effect = ValueError("fixture-private")
        result = self.fetch()
        self.assertEqual(result.status, "000")
        self.assertNotIn("fixture-private", result.error)
        self.assertEqual(len(self.policy.finished), 1)
        self.assertEqual(self.policy.active, set())
        self.open.assert_not_called()

    def test_ws_policy_load_and_finish_errors_fail_closed(self):
        client.get_policy.side_effect = RuntimeError("fixture-private")
        result = client.ws_handshake("ws://fixture.invalid/socket")
        self.assertTrue(result.policy_code)
        self.assertNotIn("fixture-private", result.error)
        self.socket.assert_not_called()
        client.get_policy.side_effect = None
        self.policy.allowance = 1
        with patch.object(self.policy, "finish", side_effect=RuntimeError("fixture-private")):
            result = client.ws_handshake("ws://fixture.invalid/socket")
        self.assertTrue(result.policy_code)
        self.assertEqual(result.status, "000")
        self.assertNotIn("fixture-private", result.error)

    def test_ws_upgrade_preserves_accept_validation_without_echoing_header(self):
        import base64
        import hashlib
        marker = "fixture-private"
        for valid in (True, False):
            with self.subTest(valid=valid):
                self.policy.allowance = 1
                def handshake_reply(size):
                    sent = self.socket.return_value.sendall.call_args.args[0].decode()
                    key = next(line.split(": ", 1)[1] for line in sent.split("\r\n")
                               if line.startswith("Sec-WebSocket-Key:"))
                    accept = base64.b64encode(hashlib.sha1(
                        (key + client._WS_GUID).encode()).digest()).decode()
                    return ("HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: "
                            + (accept if valid else marker) + "\r\n\r\n").encode()
                self.socket.return_value.recv.side_effect = handshake_reply
                result = client.ws_handshake("ws://fixture.invalid/socket")
                self.assertEqual(result.status, "101")
                self.assertEqual(bool(result.error), not valid)
                self.assertNotIn(marker, result.error)
                self.assertEqual(self.policy.finished[-1][1], "101")
                self.assertEqual(self.policy.active, set())

    def test_proxy_readiness_only_connects_to_proxy_without_sending_bytes(self):
        ok, _ = client.probe_proxy("http://127.0.0.1:12345")
        self.assertTrue(ok)
        self.socket.assert_called_once_with(("127.0.0.1", 12345), timeout=4)
        self.socket.return_value.sendall.assert_not_called()
        self.socket.return_value.send.assert_not_called()
        self.assertEqual(self.policy.reservations, [])


class RedirectPolicyTests(unittest.TestCase):
    def test_redirect_request_itself_never_constructs_followup(self):
        import urllib.request
        handler = client._NoRedirectHandler()
        for target in ("http://fixture.invalid/end", "https://other.invalid/end"):
            with self.subTest(target=target):
                request = urllib.request.Request("http://fixture.invalid/start")
                stream = io.BytesIO()
                with self.assertRaises(urllib.error.URLError):
                    handler.redirect_request(request, stream, 302, "Found", {}, target)
                self.assertTrue(stream.closed)

    def test_all_redirects_are_refused_including_same_origin(self):
        for status in (301, 302, 303, 307, 308):
            for same_origin in (True, False):
                with self.subTest(status=status, same_origin=same_origin):
                    engine = ControlledPolicy(2)
                    with local_server(lambda req: reply(req)) as (target, target_calls):
                        def respond(req):
                            if req.path == "/start":
                                reply(req, status=status, headers={
                                    "Location": "/end" if same_origin else target + "/end"})
                            else:
                                reply(req)
                        with local_server(respond) as (source, source_calls):
                            with patch.object(client, "get_policy", return_value=engine, create=True):
                                result = client.send(source + "/start", headers={}, body=b"", method="GET")
                    self.assertEqual(len(source_calls), 1)
                    self.assertEqual(target_calls, [])
                    self.assertIn("redirect", result.error.lower())
                    self.assertEqual(len(engine.reservations), 1)
                    self.assertEqual(len(engine.finished), 1)
                    self.assertEqual(engine.finished[0][1], str(status))
                    self.assertEqual(engine.active, set())


if __name__ == "__main__":
    unittest.main()
