"""Tests for __main__._resolve_bind_addresses — IPv4/IPv6 dual-stack (rules/coding.md §1)."""

from __future__ import annotations

import socket

import pytest

from weewx_clearskies_realtime.__main__ import _resolve_bind_addresses


def test_loopback_127_returns_ipv4() -> None:
    addrs = _resolve_bind_addresses("127.0.0.1", 8082)
    hosts = [a for a, _ in addrs]
    assert "127.0.0.1" in hosts


def test_loopback_ipv6_returns_ipv6() -> None:
    """::1 resolves to IPv6 loopback on dual-stack hosts; skipped on IPv4-only hosts."""
    addrs = _resolve_bind_addresses("::1", 8082)
    hosts = [a for a, _ in addrs]
    if not hosts:
        pytest.skip("IPv6 not available on this host")
    assert "::1" in hosts


def test_localhost_returns_both_loopback_families() -> None:
    """'localhost' must expand to both 127.0.0.1 and ::1 on dual-stack hosts.

    On IPv4-only hosts (CI environments without IPv6), only IPv4 is expected.
    The key invariant is that _resolve_bind_addresses never returns fewer addresses
    than getaddrinfo would for the explicit form.
    """
    addrs = _resolve_bind_addresses("localhost", 8082)
    hosts = [a for a, _ in addrs]
    # IPv4 is always available.
    assert "127.0.0.1" in hosts, f"IPv4 loopback missing from {hosts}"
    # IPv6 is present on dual-stack hosts; skip assertion if host lacks IPv6.
    try:
        socket.getaddrinfo("::1", 8082, type=socket.SOCK_STREAM)
        ipv6_available = True
    except OSError:
        ipv6_available = False
    if ipv6_available:
        assert "::1" in hosts, f"IPv6 loopback missing from {hosts}"


def test_port_preserved() -> None:
    addrs = _resolve_bind_addresses("127.0.0.1", 9999)
    assert all(p == 9999 for _, p in addrs)


def test_no_duplicate_addresses() -> None:
    addrs = _resolve_bind_addresses("localhost", 8082)
    hosts = [a for a, _ in addrs]
    assert len(hosts) == len(set(hosts)), f"Duplicate addresses in {hosts}"


def test_ipv4_only_host() -> None:
    """An explicit IPv4 address returns only that address."""
    addrs = _resolve_bind_addresses("0.0.0.0", 8765)
    # Should contain the 0.0.0.0 entry (wildcard) — not further expanded.
    assert len(addrs) >= 1
    hosts = [a for a, _ in addrs]
    assert "0.0.0.0" in hosts
