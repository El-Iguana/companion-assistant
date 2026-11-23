"""Resolve a ``.local`` name with one multicast DNS query.

A container's resolver usually has no mDNS (no nss-mdns), so ``amd-halo.local`` that works
on the host fails inside. One A-record query to 224.0.0.251:5353, first answer wins.
"""
from __future__ import annotations

import socket
import struct


def _encode(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        out += bytes([len(label)]) + label.encode()
    return out + b"\0"


def _skip_name(data: bytes, i: int) -> int:
    while True:
        n = data[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += 1 + n


def query(name: str, timeout: float = 1.5) -> str:
    """The IPv4 address for ``name``, or "" if nobody answered."""
    packet = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + _encode(name) + struct.pack(">HH", 1, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(timeout)
        s.sendto(packet, ("224.0.0.251", 5353))
        while True:
            data, _ = s.recvfrom(4096)
            _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
            if not flags & 0x8000 or not an:
                continue
            i = 12
            for _ in range(qd):
                i = _skip_name(data, i) + 4
            for _ in range(an):
                i = _skip_name(data, i)
                rtype, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
                i += 10
                if rtype == 1 and rdlen == 4:
                    return socket.inet_ntoa(data[i:i + 4])
                i += rdlen
    except (OSError, struct.error, IndexError):
        return ""
    finally:
        s.close()


def resolve(host: str) -> str:
    """``host`` as an address: the system resolver first, mDNS for a .local it can't do."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except OSError:
        if host.endswith(".local"):
            return query(host) or host
        return host
