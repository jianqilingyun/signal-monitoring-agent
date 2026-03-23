from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    pass


def validate_public_http_url(url: str) -> str:
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeUrlError("Only http:// and https:// URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise UnsafeUrlError("URL host is required")
    if _is_local_hostname(hostname):
        raise UnsafeUrlError("Local/private hosts are not allowed")

    addresses = _resolve_ip_addresses(hostname)
    for address in addresses:
        if _is_private_address(address):
            raise UnsafeUrlError("Local/private hosts are not allowed")
    return normalized


def is_loopback_host(host: str | None) -> bool:
    token = str(host or "").strip()
    if not token:
        return False
    if token in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(token).is_loopback
    except ValueError:
        return token.startswith("127.")


def _is_local_hostname(hostname: str) -> bool:
    return hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local")


def _resolve_ip_addresses(hostname: str) -> set[ipaddress._BaseAddress]:
    try:
        records = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return set()

    addresses: set[ipaddress._BaseAddress] = set()
    for row in records:
        sockaddr = row[4]
        if not sockaddr:
            continue
        candidate = str(sockaddr[0]).strip()
        if not candidate:
            continue
        try:
            addresses.add(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return addresses


def _is_private_address(address: ipaddress._BaseAddress) -> bool:
    return any(
        [
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        ]
    )
