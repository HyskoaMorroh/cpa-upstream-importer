"""Offline DNS deadline, resolver capacity and connection compatibility checks."""
import http.client
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cpa_probe import client


class DNSDeadlineTests(unittest.TestCase):
    def test_slow_dns_times_out_without_opening_socket(self):
        release = threading.Event()
        completed = threading.Event()

        def resolve(*args):
            release.wait(2)
            completed.set()
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]

        with patch.object(client.socket, 'getaddrinfo', side_effect=resolve), \
                patch.object(client.socket, 'socket') as create:
            start = time.monotonic()
            connection = client._deadline_connection(http.client.HTTPConnection, start + .05, 'fixture.invalid')
            try:
                with self.assertRaises(TimeoutError):
                    connection.connect()
                self.assertLess(time.monotonic() - start, .5)
            finally:
                release.set()
                self.assertTrue(completed.wait(1))
            create.assert_not_called()

    def test_resolver_capacity_is_bounded_and_recovers(self):
        release = threading.Event()
        completed = threading.Event()
        def resolve(*args):
            release.wait(2)
            completed.set()
            return []
        slots = threading.BoundedSemaphore(1)
        with patch.object(client, '_DNS_SLOTS', slots), \
                patch.object(client.socket, 'getaddrinfo', side_effect=resolve) as dns:
            try:
                for _ in range(3):
                    with self.assertRaises(TimeoutError):
                        client._resolve_before('fixture.invalid', 80, time.monotonic() + .03)
                self.assertEqual(dns.call_count, 1)
            finally:
                release.set()
                self.assertTrue(completed.wait(1))
            self.assertTrue(slots.acquire(timeout=1))
            slots.release()

    def test_expired_deadline_does_not_resolve(self):
        with patch.object(client.socket, 'getaddrinfo') as dns:
            with self.assertRaises(TimeoutError):
                client._resolve_before('fixture.invalid', 80, time.monotonic() - 1)
            dns.assert_not_called()

    def test_ipv6_failure_falls_back_to_ipv4_with_remaining_budget(self):
        clock = [10.0]
        first, second = Mock(), Mock()
        def fail(_):
            clock[0] += .3
            raise OSError('synthetic failure')
        first.connect.side_effect = fail
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 80, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch.object(client, '_resolve_before', return_value=addresses), \
                patch.object(client.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(client.socket, 'socket', side_effect=[first, second]):
            self.assertIs(client._connect_before(('fixture.invalid', 80), source_address=('127.0.0.1', 0), deadline=11), second)
        first.close.assert_called_once()
        second.bind.assert_called_once_with(('127.0.0.1', 0))
        self.assertAlmostEqual(second.settimeout.call_args.args[0], .7)

    def test_https_preserves_tls_hostname_through_connect_proxy(self):
        sock = Mock()
        context = Mock()
        context.wrap_socket.return_value = sock
        with patch.object(client, '_connect_before', return_value=sock) as connect, \
                patch.object(http.client.HTTPConnection, '_tunnel') as tunnel:
            connection = client._deadline_connection(http.client.HTTPSConnection,
                time.monotonic() + 2, 'proxy.invalid', context=context)
            connection.set_tunnel('upstream.invalid', 443)
            connection.connect()
            self.assertEqual(connect.call_args.args[0][0], 'proxy.invalid')
            tunnel.assert_called_once()
            context.wrap_socket.assert_called_once_with(sock, server_hostname='upstream.invalid')

    def test_dns_error_is_preserved(self):
        with patch.object(client.socket, 'getaddrinfo', side_effect=socket.gaierror('synthetic failure')):
            with self.assertRaises(socket.gaierror):
                client._resolve_before('fixture.invalid', 80, time.monotonic() + 1)

    def test_worker_start_failure_releases_capacity(self):
        slots = threading.BoundedSemaphore(1)
        with patch.object(client, '_DNS_SLOTS', slots), \
                patch.object(client.threading.Thread, 'start', side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):
                client._resolve_before('fixture.invalid', 80, time.monotonic() + 1)
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_ipv6_socket_address_is_preserved(self):
        sock = Mock()
        target = ('::1', 443, 0, 7)
        with patch.object(client, '_resolve_before', return_value=[
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', target)]), \
                patch.object(client.socket, 'socket', return_value=sock):
            self.assertIs(client._connect_before(('fixture.invalid', 443),
                deadline=time.monotonic() + 1), sock)
        sock.connect.assert_called_once_with(target)
        sock.close.assert_not_called()

    def test_direct_https_preserves_tls_hostname(self):
        sock = Mock()
        context = Mock()
        context.wrap_socket.return_value = sock
        with patch.object(client, '_connect_before', return_value=sock):
            connection = client._deadline_connection(http.client.HTTPSConnection,
                time.monotonic() + 2, 'upstream.invalid', context=context)
            connection.connect()
        context.wrap_socket.assert_called_once_with(sock, server_hostname='upstream.invalid')


if __name__ == '__main__':
    unittest.main()
