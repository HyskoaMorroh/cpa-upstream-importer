#!/usr/bin/env python3
"""HTTP transport regressions. Run directly; all servers bind to loopback."""

from __future__ import annotations

import gzip
import io
import os
import sys
import threading
import time
import tracemalloc
import unittest
import urllib.error
import urllib.request
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cpa_probe import client


def raw_deflate(data):
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


@contextmanager
def local_server(respond):
    requests = []
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append((self.path, dict(self.headers)))
            try:
                respond(self)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        do_POST = do_HEAD = do_GET
        do_CONNECT = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.stop = stop
    worker = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def reply(request, body=b'{"ok":true}', *, status=200, headers=None, length=None):
    request.send_response(status)
    request.send_header("Content-Length", str(len(body) if length is None else length))
    for name, value in (headers or {}).items():
        request.send_header(name, value)
    request.end_headers()
    if request.command != "HEAD":
        request.wfile.write(body)


def fetch(url, **kwargs):
    return client.send(url, headers=kwargs.pop("headers", {}), body=b"",
                       method=kwargs.pop("method", "GET"), **kwargs)


class TransportComplianceTests(unittest.TestCase):
    def test_unicode_json_is_valid_uncompressed_text(self):
        for codepoint in (0x00A0, 0x200D):
            text = '{"content":"left' + chr(codepoint) + 'right"}'
            for status in (200, 403):
                with self.subTest(codepoint=codepoint, status=status):
                    with local_server(
                        lambda request: reply(
                            request, text.encode("utf-8"), status=status
                        )
                    ) as (url, _):
                        result = fetch(url)
                    self.assertEqual(result.status, str(status))
                    self.assertEqual(result.body, text)
                    self.assertEqual(result.error, "")

    def test_short_unlabelled_truncated_deflate_rejects_plaintext_fallback(self):
        payload = raw_deflate(b'{"ok":true}')[:-1]
        for status in (200, 403):
            with self.subTest(status=status):
                with local_server(
                    lambda request: reply(request, payload, status=status)
                ) as (url, _):
                    result = fetch(url)
                self.assertEqual(result.status, "000" if status == 200 else "403")
                self.assertTrue(result.error)
                self.assertEqual(result.body, "")

    def test_many_empty_gzip_members_stop_during_decode(self):
        payload = gzip.compress(b"") * 180000 + gzip.compress(b'{"ok":true}')
        # Enter decoding before the clock expires, independently of socket speed.
        ticks = iter((0.0, 0.0, 2.0))
        with patch.object(client.time, "monotonic",
                          side_effect=lambda: next(ticks, 2.0)):
            with self.assertRaises(TimeoutError):
                client._decode_body(payload, strict=True, deadline=1.0)

    def test_many_empty_gzip_members_share_request_budget(self):
        payload = gzip.compress(b"") * 180000 + gzip.compress(b'{"ok":true}')
        with local_server(lambda request: reply(request, payload)) as (url, _):
            started = time.monotonic()
            result = fetch(url, timeout=0.1)
            elapsed = time.monotonic() - started
        self.assertEqual(result.status, "000")
        self.assertIn("timeout", result.error.lower())
        self.assertLess(elapsed, 0.8)
        self.assertLess(result.elapsed_ms, 800)

    def test_connect_and_tls_handshake_share_remaining_budget(self):
        def proxy(request):
            if request.server.stop.wait(0.3):
                return
            request.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            request.wfile.flush()
            request.server.stop.wait(2)

        with local_server(proxy) as (url, received):
            # Ignore host bypass rules only for this loopback proxy fixture.
            with patch("urllib.request.proxy_bypass", return_value=False):
                started = time.monotonic()
                result = fetch("https://127.0.0.1:443/", proxy=url, timeout=0.4)
                elapsed = time.monotonic() - started
        # Under load the shared deadline can expire before CONNECT completes.
        self.assertLessEqual(len(received), 1)
        self.assertEqual(result.status, "000")
        self.assertRegex(result.error.lower(), r"timeout|timed out")
        self.assertLess(elapsed, 0.8)
        self.assertLess(result.elapsed_ms, 800)

    def test_connect_refreshes_remaining_tls_budget_deterministically(self):
        clock = [0.0]

        class FakeSocket:
            def __init__(self, timeout):
                self.timeout = timeout

            def settimeout(self, timeout):
                self.timeout = timeout

        class FakeConnection:
            def __init__(self, host, **kwargs):
                self.sock = FakeSocket(kwargs["timeout"])

            def _tunnel(self):
                clock[0] += 0.3

        with patch.object(client.time, "monotonic", side_effect=lambda: clock[0]):
            connection = client._deadline_connection(
                FakeConnection, 0.4, "proxy.example.invalid"
            )
            connection._tunnel()
            self.assertAlmostEqual(connection.sock.timeout, 0.1)
            with self.assertRaises(TimeoutError):
                connection._tunnel()

    def test_bodyless_responses_ignore_content_encoding(self):
        for method, status in (("HEAD", 200), ("HEAD", 403),
                               ("GET", 204), ("GET", 304)):
            for encoding in ("br", "zstd", "gzip", "deflate"):
                with self.subTest(method=method, status=status, encoding=encoding):
                    def respond(request):
                        reply(request, b"", status=status,
                              headers={"Content-Encoding": encoding},
                              length=123 if method == "HEAD" or status == 304 else 0)

                    with local_server(respond) as (url, _):
                        result = fetch(url, method=method)
                    self.assertEqual((result.status, result.body, result.error),
                                     (str(status), "", ""))

    def test_zlib_magic_plaintext_fallback_requires_valid_text(self):
        cases = [
            (b'x^metadata: ok\n\ndata: {"ok":true}\n\n', True),
            (b"x^ordinary readable plaintext", True),
            (b"x^", True),
            (b"x^\x00binary", False),
            (b"x^\xffbinary", False),
            (zlib.compress(b'{"ok":true}')[:-2], False),
        ]
        for status in (200, 403):
            for payload, valid in cases:
                with self.subTest(status=status, payload=payload):
                    with local_server(
                        lambda request: reply(request, payload, status=status)
                    ) as (url, _):
                        result = fetch(url)
                    self.assertEqual(result.status,
                                     str(status) if valid or status == 403 else "000")
                    self.assertEqual(bool(result.error), not valid)
                    self.assertEqual(result.body, payload.decode() if valid else "")

    def test_cross_origin_redirects_never_reach_destination(self):
        credentials = {
            "Authorization": "Bearer fixture-only",
            "x-api-key": "fixture-only",
            "x-goog-api-key": "fixture-only",
        }
        with local_server(reply) as (destination, received):
            for status, method in [(301, "GET"), (302, "GET"), (303, "GET"),
                                   (307, "GET"), (308, "GET"), (302, "POST")]:
                with self.subTest(status=status, method=method):
                    def redirect(request):
                        reply(request, b"", status=status,
                              headers={"Location": destination + "/sink"})

                    with local_server(redirect) as (source, _):
                        result = fetch(source, headers=credentials, method=method)
                    self.assertEqual(len(received), 0)
                    self.assertEqual(result.status, "000")
                    self.assertIn("redirect", result.error.lower())

    def test_same_origin_redirect_preserves_headers_and_no_implicit_user_agent(self):
        def respond(request):
            if request.path == "/start":
                reply(request, b"", status=302, headers={"Location": "/final"})
            else:
                reply(request)

        with local_server(respond) as (url, received):
            result = fetch(url + "/start", headers={"x-api-key": "fixture-only"})
        self.assertEqual(result.status, "200")
        self.assertEqual(result.error, "")
        self.assertEqual(result.body, '{"ok":true}')
        self.assertEqual([path for path, _ in received], ["/start", "/final"])
        final_headers = {key.lower(): value for key, value in received[-1][1].items()}
        self.assertEqual(final_headers["x-api-key"], "fixture-only")
        self.assertNotIn("user-agent", final_headers)

    def test_redirect_origin_uses_scheme_host_and_effective_port(self):
        handler = next(h for h in client._opener(None).handlers
                       if isinstance(h, urllib.request.HTTPRedirectHandler))
        request = urllib.request.Request("https://EXAMPLE.invalid:443/start")
        redirected = handler.redirect_request(
            request, io.BytesIO(), 302, "Found", {}, "https://example.invalid/end"
        )
        self.assertEqual(redirected.full_url, "https://example.invalid/end")
        for target in ("http://example.invalid/end",
                       "https://example.invalid:444/end",
                       "https://other.invalid/end"):
            with self.subTest(target=target):
                with self.assertRaises(urllib.error.URLError):
                    handler.redirect_request(request, io.BytesIO(), 302, "Found",
                                             {}, target)

    def test_compressed_bombs_are_transport_failures(self):
        expanded = b"a" * (client.READ_LIMIT * 2)
        for encode in (gzip.compress, zlib.compress, raw_deflate):
            with self.subTest(encoding=encode.__name__):
                payload = encode(expanded)
                self.assertLess(len(payload), client.READ_LIMIT)
                with local_server(lambda request: reply(request, payload)) as (url, _):
                    result = fetch(url)
                self.assertEqual(result.status, "000")
                self.assertTrue(result.error)
                self.assertLessEqual(len(result.body.encode()), client.READ_LIMIT)

    def test_decompression_allocations_are_bounded(self):
        expanded = b"a" * (client.READ_LIMIT * 2)
        for encode in (gzip.compress, zlib.compress, raw_deflate):
            with self.subTest(encoding=encode.__name__):
                payload = encode(expanded)
                tracemalloc.start()
                try:
                    client._decode_body(payload)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, client.READ_LIMIT * 3)

    def test_wire_and_decoded_limits_include_exact_boundary(self):
        for encode in (bytes, gzip.compress, zlib.compress, raw_deflate):
            for extra in (0, 1):
                with self.subTest(encoding=encode.__name__, extra=extra):
                    payload = encode(b"a" * (client.READ_LIMIT + extra))
                    with local_server(lambda request: reply(request, payload)) as (url, _):
                        result = fetch(url)
                    self.assertEqual(result.status, "000" if extra else "200")
                    self.assertEqual(bool(result.error), bool(extra))
                    if not extra:
                        self.assertEqual(len(result.body), client.READ_LIMIT)

    def test_corrupt_and_unsupported_compression_is_explicit(self):
        text = b'{"ok":true}'
        cases = [
            (gzip.compress(text)[:-4], {}),
            (gzip.compress(text)[:-8] + b"\x00" * 8, {}),
            (zlib.compress(text)[:-2], {}),
            (raw_deflate(text)[:-1], {"Content-Encoding": "deflate"}),
            (b"\x28\xb5\x2f\xfd" + b"\x00" * 32, {}),
            (b"opaque compressed payload", {"Content-Encoding": "br"}),
            (b"", {"Content-Encoding": "unsupported"}),
            (bytes(range(0x80, 0xC0)) * 4, {}),
        ]
        for index, (payload, headers) in enumerate(cases):
            with self.subTest(case=index):
                with local_server(
                    lambda request: reply(request, payload, headers=headers)
                ) as (url, _):
                    result = fetch(url)
                self.assertEqual(result.status, "000")
                self.assertTrue(result.error)
                self.assertEqual(result.body, "")

    def test_concatenated_gzip_members_share_one_output_limit(self):
        for part in (b'{"ok":true}', b"a" * (client.READ_LIMIT // 2 + 1)):
            payload = gzip.compress(part) * 2
            with self.subTest(size=len(part)):
                with local_server(lambda request: reply(request, payload)) as (url, _):
                    result = fetch(url)
                if len(part) * 2 <= client.READ_LIMIT:
                    self.assertEqual(result.body, (part * 2).decode())
                    self.assertEqual(result.error, "")
                else:
                    self.assertEqual(result.status, "000")
                    self.assertTrue(result.error)

    def test_gzip_member_boundaries_padding_and_trailers(self):
        first = gzip.compress(b"first")
        second = gzip.compress(b"second")
        cases = [
            (first + b"\x00" * 65536 + second + b"\x00" * 10, "firstsecond"),
            (first + second[:-1], None),
            (first + second[:-8] + b"\x00" * 8, None),
            (first + b"junk", None),
        ]
        for payload, expected in cases:
            with self.subTest(expected=expected, size=len(payload)):
                if expected is None:
                    with self.assertRaises((ValueError, zlib.error)):
                        client._decode_body(payload, strict=True)
                else:
                    self.assertEqual(client._decode_body(payload, strict=True), expected)

    def test_normal_json_sse_http_errors_and_mislabelled_plaintext(self):
        text = b'{"error":{"message":"fixture"}}'
        cases = [(text, {}), (gzip.compress(text), {}),
                 (zlib.compress(text), {}), (raw_deflate(text), {}),
                 (text, {"Content-Encoding": "gzip"}),
                 (b"data: {\"ok\":true}\n\ndata: [DONE]\n\n",
                  {"Content-Type": "text/event-stream"})]
        for status in (200, 403):
            for payload, headers in cases:
                with self.subTest(status=status, headers=headers, size=len(payload)):
                    with local_server(
                        lambda request: reply(request, payload, status=status,
                                              headers=headers)
                    ) as (url, _):
                        result = fetch(url)
                    self.assertEqual(result.status, str(status))
                    self.assertEqual(result.error, "")
                    expected = payload if "Content-Type" in headers else text
                    self.assertEqual(result.body, expected.decode())

    def test_short_content_length_is_not_a_complete_response(self):
        for status in (200, 403):
            with self.subTest(status=status):
                with local_server(
                    lambda request: reply(request, b"{}", status=status, length=20)
                ) as (url, _):
                    result = fetch(url)
                self.assertEqual(result.status, "000" if status == 200 else "403")
                self.assertTrue(result.error)

    def test_http_error_with_bad_compression_keeps_status_and_error(self):
        for payload in (gzip.compress(b"{}")[:-4],
                        gzip.compress(b"a" * (client.READ_LIMIT + 1))):
            with self.subTest(size=len(payload)):
                with local_server(
                    lambda request: reply(request, payload, status=403)
                ) as (url, _):
                    result = fetch(url)
                self.assertEqual(result.status, "403")
                self.assertTrue(result.error)
                self.assertEqual(result.body, "")

    def test_head_does_not_require_the_declared_body(self):
        with local_server(lambda request: reply(request, length=100)) as (url, _):
            result = fetch(url, method="HEAD")
        self.assertEqual((result.status, result.body, result.error), ("200", "", ""))

    def test_drip_body_respects_whole_request_deadline(self):
        for status in (200, 403):
            for chunked in (False, True):
                with self.subTest(status=status, chunked=chunked):
                    def drip(request):
                        request.send_response(status)
                        request.send_header("Transfer-Encoding" if chunked else "Content-Length",
                                            "chunked" if chunked else "20")
                        request.end_headers()
                        for _ in range(20):
                            request.wfile.write(b"1\r\nx\r\n" if chunked else b"x")
                            request.wfile.flush()
                            if request.server.stop.wait(0.1):
                                return
                        if chunked:
                            request.wfile.write(b"0\r\n\r\n")

                    with local_server(drip) as (url, _):
                        started = time.monotonic()
                        result = fetch(url, timeout=0.4)
                        elapsed = time.monotonic() - started
                    self.assertLess(elapsed, 1.1)
                    self.assertLess(result.elapsed_ms, 1100)
                    self.assertEqual(result.status, "000" if status == 200 else "403")
                    self.assertIn("timeout", result.error.lower())

    def test_drip_headers_respect_deadline(self):
        def drip(request):
            request.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nX-Drip: ")
            for _ in range(20):
                request.wfile.write(b"x")
                request.wfile.flush()
                if request.server.stop.wait(0.1):
                    return
            request.wfile.write(b"\r\n\r\n")

        with local_server(drip) as (url, _):
            started = time.monotonic()
            result = fetch(url, timeout=0.4)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.1)
        self.assertEqual(result.status, "000")
        self.assertIn("timeout", result.error.lower())

    def test_redirect_hops_share_the_original_deadline(self):
        def respond(request):
            if request.server.stop.wait(0.2):
                return
            hop = int(request.path[1:])
            if hop == 3:
                reply(request)
            else:
                reply(request, b"", status=302,
                      headers={"Location": f"/{hop + 1}"})

        with local_server(respond) as (url, _):
            started = time.monotonic()
            result = fetch(url + "/0", timeout=0.4)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.65)
        self.assertEqual(result.status, "000")
        self.assertIn("timeout", result.error.lower())

    def test_redirect_body_is_not_drained_without_a_bound(self):
        def respond(request):
            if request.path == "/final":
                reply(request)
                return
            request.send_response(302)
            request.send_header("Location", "/final")
            request.send_header("Content-Length", "20")
            request.end_headers()
            for _ in range(20):
                request.wfile.write(b"x")
                request.wfile.flush()
                if request.server.stop.wait(0.1):
                    return

        with local_server(respond) as (url, _):
            started = time.monotonic()
            result = fetch(url + "/start", timeout=0.4)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.1)
        self.assertEqual(result.status, "200")
        self.assertEqual(result.body, '{"ok":true}')


if __name__ == "__main__":
    unittest.main()
