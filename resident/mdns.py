"""Resolve a ``.local`` name with one multicast DNS query.

A container's resolver usually has no mDNS (no nss-mdns), so ``amd-halo.local`` that works
on the host fails inside. One A-record query to 224.0.0.251:5353, first answer wins.

The question goes out of each LAN interface by name, not just where the routes say: with a
VPN up (Proton on predator, #110) the routes send multicast into the tunnel, where nobody
answers. The last answer is remembered, for when nobody answers at all.
"""
from __future__ import annotations

import json
import socket
import struct

_SIOCGIFFLAGS, _SIOCGIFADDR = 0x8913, 0x8915
_IFF_UP, _IFF_LOOPBACK, _IFF_POINTOPOINT, _IFF_MULTICAST = 0x1, 0x8, 0x10, 0x1000


def lan_addresses() -> list[str]:
    """IPv4 addresses of the interfaces mDNS can be on: up, multicast, not loopback and not
    point-to-point (a VPN tunnel). Empty where this can't be asked (not Linux)."""
    try:
        import fcntl
        names = [n for _, n in socket.if_nameindex()]
    except (ImportError, OSError):
        return []
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            req = struct.pack("256s", name.encode()[:15])
            try:
                flags = struct.unpack("H", fcntl.ioctl(s.fileno(), _SIOCGIFFLAGS, req)[16:18])[0]
                if not flags & _IFF_UP or not flags & _IFF_MULTICAST or flags & (_IFF_LOOPBACK | _IFF_POINTOPOINT):
                    continue
                out.append(socket.inet_ntoa(fcntl.ioctl(s.fileno(), _SIOCGIFADDR, req)[20:24]))
            except OSError:          # no IPv4 address on it
                continue
    finally:
        s.close()
    return out


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
        for addr in lan_addresses():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
                s.sendto(packet, ("224.0.0.251", 5353))
            except OSError:
                continue
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("0.0.0.0"))
        s.sendto(packet, ("224.0.0.251", 5353))          # and where the routes say, as before
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


def _cache_path():
    from resident import config
    return config.data_dir() / "addresses.json"


def _cached() -> dict:
    try:
        d = json.loads(_cache_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(host: str, addr: str) -> None:
    d = _cached()
    if d.get(host) == addr:
        return
    d[host] = addr
    try:
        p = _cache_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(p)
    except OSError:
        pass


def resolve(host: str) -> str:
    """``host`` as an address: the system resolver first, mDNS for a .local it can't do, and
    for a .local nobody answered for, the address it had last time."""
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except OSError:
        if not host.endswith(".local"):
            return host
    addr = query(host)
    if addr:
        _remember(host, addr)
        return addr
    return _cached().get(host) or host
