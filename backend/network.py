"""Discover local IPv4 URLs that a bound listener can serve."""

from __future__ import annotations

import ipaddress
import socket


def _lan_address(value: str) -> ipaddress.IPv4Address | None:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return None
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
    ):
        return None
    return address


def discover_lan_ipv4() -> list[str]:
    """Return distinct local IPv4 addresses, including virtual adapters.

    Hostname lookup uses the operating system's local interface information;
    it does not depend on an external IP lookup service or an internet route.
    """
    try:
        records = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM
        )
    except OSError:
        return []

    addresses = {
        address
        for family, _kind, _protocol, _name, endpoint in records
        if family == socket.AF_INET
        if (address := _lan_address(endpoint[0])) is not None
    }
    return [str(address) for address in sorted(addresses)]


def lan_urls_for_host(host: str, port: int) -> list[str]:
    """Return LAN origins for a resolved numeric bind address and port."""
    if host == "0.0.0.0":
        addresses = discover_lan_ipv4()
    else:
        address = _lan_address(host)
        addresses = [str(address)] if address is not None else []
    return [f"http://{address}:{port}" for address in addresses]


def lan_urls(listener: socket.socket) -> list[str]:
    """Return LAN origins reachable through the listener's actual binding.

    Only an IPv4 wildcard listener serves every discovered IPv4 interface.
    An explicit IPv4 listener serves its own address, while loopback and IPv6
    listeners must not advertise unrelated IPv4 addresses.
    """
    if listener.family != socket.AF_INET:
        return []
    try:
        host, port = listener.getsockname()
    except OSError:
        return []
    return lan_urls_for_host(host, port)
