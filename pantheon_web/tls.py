"""HTTPS for other devices (#7): Cantinero's own small certificate authority.

Browsers allow the microphone only in a secure context: https://, or this computer's own
127.0.0.1 / localhost. So a phone or another PC can chat over plain HTTP but cannot talk.
This makes, once, under ``data/tls/``:

* **a local CA** (10 years). Each device trusts it once (the ``/ca`` page explains how).
  It is **name-constrained**: it can only vouch for ``localhost``, ``.local`` names, the
  names given in ``PANTHEON_TLS_NAMES`` and private addresses (10/8, 172.16/12, 192.168/16,
  127/8). A copy of its key could not impersonate a real website to a device that trusts it.
* **this server's certificate**, signed by it, for those names and addresses: 397 days,
  remade on start when it is near its end or the names changed (a new IP from DHCP).

Made with the ``openssl`` command (in the image; no Python dependency). ``service.py``
serves HTTPS with it on ``PANTHEON_HTTPS_PORT`` beside plain HTTP.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import ssl
import subprocess
import tempfile
from pathlib import Path

LEAF_DAYS = 397                  # browsers distrust longer server certificates
RENEW_DAYS = 30                  # remade when fewer than this are left
CA_DAYS = 3650
PRIVATE_NETS = ("10.0.0.0/255.0.0.0", "172.16.0.0/255.240.0.0", "192.168.0.0/255.255.0.0",
                "127.0.0.0/255.0.0.0")


def tls_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "tls"


_PRIVATE = [ipaddress.ip_network(n.replace("/255.0.0.0", "/8").replace("/255.240.0.0", "/12")
                                   .replace("/255.255.0.0", "/16")) for n in PRIVATE_NETS]
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"


def split_names(names, allowed_single=None) -> tuple[list[str], list[str]]:
    """(DNS names, IP addresses) the certificate may carry: localhost and 127.0.0.1 always;
    `.local` names; single-label names (the computer's own name) the CA was made for
    (`allowed_single`, None = any); private IPv4 addresses. Anything else would put a name
    outside the CA's constraints into the certificate, and browsers then refuse all of it,
    so it is left out (and logged)."""
    dns, ips, dropped = ["localhost"], ["127.0.0.1"], []
    for raw in names or []:
        n = str(raw).strip().strip(".").lower()
        if not n:
            continue
        try:
            ip = ipaddress.ip_address(n)
            if ip.version == 4 and any(ip in net for net in _PRIVATE):
                if str(ip) not in ips:
                    ips.append(str(ip))
            else:
                dropped.append(n)
            continue
        except ValueError:
            pass
        ok = (re.fullmatch(rf"{_LABEL}(\.{_LABEL})*\.local", n)
              or (re.fullmatch(_LABEL, n) and (allowed_single is None or n in allowed_single)))
        if ok and n not in dns:
            dns.append(n)
        elif not ok:
            dropped.append(n)
    if dropped:
        print(f"[tls] left out of the certificate (outside what the local CA may vouch for): "
              f"{', '.join(dropped)}", flush=True)
    return dns, ips


def _run(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def _ca_conf(dns: list[str]) -> str:
    # The CA may sign only these: localhost, .local names, the given names, private nets.
    single = sorted({d for d in dns if d not in ("localhost",) and not d.endswith(".local")})
    permitted = ["DNS:localhost", "DNS:local"] + [f"DNS:{d}" for d in single] + \
                [f"IP:{n}" for n in PRIVATE_NETS]
    return ("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\n"
            "CN=Cantinero local CA\nO=Cantinero\n[ext]\n"
            "basicConstraints=critical,CA:TRUE,pathlen:0\n"
            "keyUsage=critical,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\n"
            f"nameConstraints=critical,{','.join('permitted;' + p for p in permitted)}\n")


def _make_ca(d: Path, dns: list[str]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as f:
        f.write(_ca_conf(dns))
    try:
        _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(d / "ca.key"))
        os.chmod(d / "ca.key", 0o600)
        _run("req", "-x509", "-new", "-key", str(d / "ca.key"), "-sha256", "-days", str(CA_DAYS),
             "-config", f.name, "-out", str(d / "ca.crt"))
    finally:
        os.unlink(f.name)


def _make_leaf(d: Path, dns: list[str], ips: list[str]) -> None:
    san = ",".join([f"DNS:{n}" for n in dns] + [f"IP:{i}" for i in ips])
    ext = ("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
           f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n"
           "authorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    with tempfile.NamedTemporaryFile("w", suffix=".ext", delete=False) as f:
        f.write(ext)
    try:
        _run("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(d / "server.key"))
        os.chmod(d / "server.key", 0o600)
        _run("req", "-new", "-key", str(d / "server.key"), "-subj", f"/CN={dns[-1]}",
             "-out", str(d / "server.csr"))
        _run("x509", "-req", "-in", str(d / "server.csr"), "-CA", str(d / "ca.crt"),
             "-CAkey", str(d / "ca.key"), "-CAcreateserial", "-days", str(LEAF_DAYS),
             "-sha256", "-extfile", f.name, "-out", str(d / "server.crt"))
    finally:
        os.unlink(f.name)
        (d / "server.csr").unlink(missing_ok=True)
    (d / "server.names.json").write_text(json.dumps({"dns": dns, "ips": ips}))


def _expires_soon(cert: Path) -> bool:
    r = subprocess.run(["openssl", "x509", "-checkend", str(RENEW_DAYS * 86400), "-noout",
                        "-in", str(cert)], capture_output=True)
    return r.returncode != 0


def ensure(data_dir: Path, names) -> tuple[Path, Path]:
    """(certificate, key) for HTTPS, making or renewing them as needed."""
    d = tls_dir(data_dir)
    d.mkdir(parents=True, exist_ok=True)
    if not (d / "ca.crt").exists() or not (d / "ca.key").exists():
        dns, _ = split_names(names)
        _make_ca(d, dns)
        # The single-label names it may vouch for are fixed now: devices trust this CA.
        (d / "ca.names.json").write_text(json.dumps(
            sorted(n for n in dns if n != "localhost" and "." not in n)))
        for f in ("server.crt", "server.key", "server.names.json"):
            (d / f).unlink(missing_ok=True)
    try:
        single = set(json.loads((d / "ca.names.json").read_text()))
    except (OSError, ValueError):
        single = set()
    dns, ips = split_names(names, single)
    stale = True
    if (d / "server.crt").exists() and (d / "server.names.json").exists():
        try:
            had = json.loads((d / "server.names.json").read_text())
            stale = had != {"dns": dns, "ips": ips} or _expires_soon(d / "server.crt")
        except (ValueError, OSError, KeyError):
            stale = True
    if stale:
        _make_leaf(d, dns, ips)
    return d / "server.crt", d / "server.key"


def ca_pem(data_dir: Path) -> bytes:
    return (tls_dir(data_dir) / "ca.crt").read_bytes()


def fingerprint(pem: bytes) -> str:
    """SHA-256 of the certificate, as phones and browsers show it (AB:CD:…)."""
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
