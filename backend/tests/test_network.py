from __future__ import annotations

import socket
import unittest
from unittest import mock

from backend.network import discover_lan_ipv4, lan_urls, lan_urls_for_host


def ipv4_record(address: str) -> tuple:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))


class NetworkTests(unittest.TestCase):
    def listener(self, host: str, port: int = 8000, family: int = socket.AF_INET):
        listener = mock.Mock(spec=socket.socket)
        listener.family = family
        listener.getsockname.return_value = (host, port)
        return listener

    def test_discovery_lists_distinct_physical_and_virtual_interfaces(self) -> None:
        records = [ipv4_record(address) for address in (
            "192.168.56.1", "192.168.1.156", "192.168.56.1", "10.0.0.10",
            "10.0.0.2", "169.254.1.2",
        )]
        with mock.patch("backend.network.socket.gethostname", return_value="desktop"), \
             mock.patch("backend.network.socket.getaddrinfo", return_value=records) as lookup:
            self.assertEqual(discover_lan_ipv4(), [
                "10.0.0.2", "10.0.0.10", "169.254.1.2", "192.168.1.156", "192.168.56.1",
            ])
            lookup.assert_called_once_with("desktop", None, socket.AF_INET, socket.SOCK_STREAM)

    def test_discovery_excludes_addresses_that_are_not_lan_interfaces(self) -> None:
        records = [ipv4_record(address) for address in (
            "127.0.0.1", "127.0.0.2", "0.0.0.0", "224.0.0.1", "255.255.255.255",
            "invalid", "192.168.1.156",
        )]
        records.append((socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)))
        with mock.patch("backend.network.socket.getaddrinfo", return_value=records):
            self.assertEqual(discover_lan_ipv4(), ["192.168.1.156"])

    def test_discovery_lookup_failure_does_not_prevent_startup(self) -> None:
        with mock.patch("backend.network.socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertEqual(discover_lan_ipv4(), [])
        with mock.patch("backend.network.socket.gethostname", side_effect=OSError):
            self.assertEqual(discover_lan_ipv4(), [])

    def test_wildcard_listener_advertises_each_interface_at_actual_port(self) -> None:
        with mock.patch("backend.network.discover_lan_ipv4", return_value=[
            "192.168.1.156", "192.168.56.1",
        ]):
            self.assertEqual(lan_urls(self.listener("0.0.0.0", 4321)), [
                "http://192.168.1.156:4321", "http://192.168.56.1:4321",
            ])

    def test_explicit_interface_only_advertises_its_bound_address(self) -> None:
        with mock.patch("backend.network.discover_lan_ipv4") as discover:
            self.assertEqual(lan_urls(self.listener("192.168.56.1", 9876)), [
                "http://192.168.56.1:9876",
            ])
            discover.assert_not_called()

    def test_loopback_and_ipv6_listeners_do_not_advertise_ipv4_interfaces(self) -> None:
        with mock.patch("backend.network.discover_lan_ipv4") as discover:
            for host, family in (
                ("127.0.0.1", socket.AF_INET), ("127.0.0.2", socket.AF_INET),
                ("::", socket.AF_INET6), ("::1", socket.AF_INET6),
                ("fe80::1%12", socket.AF_INET6),
            ):
                with self.subTest(host=host):
                    self.assertEqual(lan_urls(self.listener(host, family=family)), [])
            discover.assert_not_called()

    def test_unavailable_socket_address_returns_no_urls(self) -> None:
        listener = self.listener("0.0.0.0")
        listener.getsockname.side_effect = OSError
        self.assertEqual(lan_urls(listener), [])

    def test_resolved_host_urls_match_listener_urls(self) -> None:
        with mock.patch("backend.network.discover_lan_ipv4", return_value=["192.168.1.156"]):
            self.assertEqual(lan_urls_for_host("0.0.0.0", 4321), ["http://192.168.1.156:4321"])
            self.assertEqual(lan_urls_for_host("192.168.56.1", 9876), ["http://192.168.56.1:9876"])
            for host in ("127.0.0.1", "::", "fe80::1%12", "localhost"):
                with self.subTest(host=host):
                    self.assertEqual(lan_urls_for_host(host, 8000), [])


if __name__ == "__main__":
    unittest.main()
