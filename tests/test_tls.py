"""HTTPS for other devices (#7): Cantinero's own name-constrained CA and the /ca page.

    .venv/bin/python -m pytest -q tests/test_tls.py
"""
from __future__ import annotations

import json
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pantheon_web import tls

NAMES = ["amd-halo", "amd-halo.local", "192.168.1.236", "fe80::1", "8.8.8.8", "evil.com"]


def _ext(cert, name):
    return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", name],
                          capture_output=True, text=True).stdout


def _verifies(ca, cert):
    return subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(cert)],
                          capture_output=True).returncode == 0


def test_names_outside_the_cas_reach_are_left_out():
    dns, ips = tls.split_names(NAMES)
    assert dns == ["localhost", "amd-halo", "amd-halo.local"]
    assert ips == ["127.0.0.1", "192.168.1.236"]
    assert tls.split_names(["other-box"], {"amd-halo"})[0] == ["localhost"]


def test_the_certificate_covers_the_names_and_verifies(tmp_path):
    cert, key = tls.ensure(tmp_path, NAMES)
    san = _ext(cert, "subjectAltName")
    for n in ("DNS:amd-halo.local", "DNS:amd-halo", "IP Address:192.168.1.236", "IP Address:127.0.0.1"):
        assert n in san
    assert "evil.com" not in san and "8.8.8.8" not in san
    assert _verifies(tmp_path / "tls" / "ca.crt", cert)
    assert (key.stat().st_mode & 0o777) == 0o600
    assert ((tmp_path / "tls" / "ca.key").stat().st_mode & 0o777) == 0o600


def test_it_is_reused_and_remade_when_the_address_changes(tmp_path):
    cert, _ = tls.ensure(tmp_path, NAMES)
    before = cert.read_bytes()
    assert tls.ensure(tmp_path, NAMES)[0].read_bytes() == before
    ca = (tmp_path / "tls" / "ca.crt").read_bytes()
    tls.ensure(tmp_path, ["amd-halo", "amd-halo.local", "192.168.1.99"])
    assert "192.168.1.99" in _ext(cert, "subjectAltName")
    assert (tmp_path / "tls" / "ca.crt").read_bytes() == ca          # devices keep trusting it


def test_the_ca_cannot_vouch_for_a_real_website(tmp_path):
    tls.ensure(tmp_path, NAMES)
    d = tmp_path / "tls"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                    "-out", str(tmp_path / "k")], check=True)
    subprocess.run(["openssl", "req", "-new", "-key", str(tmp_path / "k"), "-subj", "/CN=www.bank.com",
                    "-out", str(tmp_path / "csr")], check=True)
    (tmp_path / "ext").write_text("subjectAltName=DNS:www.bank.com\n")
    subprocess.run(["openssl", "x509", "-req", "-in", str(tmp_path / "csr"), "-CA", str(d / "ca.crt"),
                    "-CAkey", str(d / "ca.key"), "-CAcreateserial", "-days", "30",
                    "-extfile", str(tmp_path / "ext"), "-out", str(tmp_path / "forged.crt")],
                   check=True, capture_output=True)
    assert not _verifies(d / "ca.crt", tmp_path / "forged.crt")
    assert "Permitted" in _ext(d / "ca.crt", "nameConstraints")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import main
    from pantheon_web.tls_page import router

    monkeypatch.setattr(main, "DATA", tmp_path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_setup_page_and_download(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PANTHEON_HTTPS_PORT", "0")
    assert "HTTPS is off" in client.get("/ca").text and client.get("/ca.crt").status_code == 404
    monkeypatch.setenv("PANTHEON_HTTPS_PORT", "7868")
    tls.ensure(tmp_path, NAMES)
    page = client.get("/ca", headers={"host": "amd-halo.local:7867"}).text
    assert "https://amd-halo.local:7868/" in page and "/ca.crt" in page
    assert tls.fingerprint(tls.ca_pem(tmp_path)) in page
    r = client.get("/ca.crt")
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-x509-ca-cert"
    assert r.content.startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" not in r.content
