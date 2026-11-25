import socket

from resident import mdns


def _no_system_resolver(monkeypatch):
    def fail(*a, **kw):
        raise OSError("no mdns here")
    monkeypatch.setattr(mdns.socket, "getaddrinfo", fail)


def test_lan_addresses_skips_loopback_and_tunnels():
    addrs = mdns.lan_addresses()
    assert "127.0.0.1" not in addrs
    assert all(socket.inet_aton(a) for a in addrs)


def test_query_goes_out_of_each_lan_interface(monkeypatch):
    sent = []

    class FakeSocket:
        def __init__(self, *a): self.iface = "0.0.0.0"
        def settimeout(self, t): pass
        def setsockopt(self, level, opt, val): self.iface = socket.inet_ntoa(val)
        def sendto(self, data, dest): sent.append((self.iface, dest))
        def recvfrom(self, n): raise socket.timeout()
        def close(self): pass
    monkeypatch.setattr(mdns, "lan_addresses", lambda: ["192.168.1.229", "10.0.0.7"])
    monkeypatch.setattr(mdns.socket, "socket", FakeSocket)
    assert mdns.query("halo.local", 0.1) == ""
    assert [i for i, _ in sent] == ["192.168.1.229", "10.0.0.7", "0.0.0.0"]
    assert all(d == ("224.0.0.251", 5353) for _, d in sent)


def test_resolve_remembers_the_last_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENT_DATA", str(tmp_path))
    _no_system_resolver(monkeypatch)
    monkeypatch.setattr(mdns, "query", lambda h: "192.168.1.236")
    assert mdns.resolve("halo.local") == "192.168.1.236"
    monkeypatch.setattr(mdns, "query", lambda h: "")          # a VPN came up: nobody answers
    assert mdns.resolve("halo.local") == "192.168.1.236"
    assert mdns.resolve("other.local") == "other.local"        # never seen: the name, as before


def test_resolve_leaves_other_names_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("RESIDENT_DATA", str(tmp_path))
    _no_system_resolver(monkeypatch)
    monkeypatch.setattr(mdns, "query", lambda h: (_ for _ in ()).throw(AssertionError("asked")))
    assert mdns.resolve("halo.example") == "halo.example"
