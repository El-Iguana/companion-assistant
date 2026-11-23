"""``python -m resident``: pair with a Cantinero server, check it, or serve.

    python -m resident pair amd-halo.local [--name Predator]
    python -m resident status
    python -m resident serve          # 127.0.0.1:$RESIDENT_PORT (7867)
"""
from __future__ import annotations

import argparse
import socket
import sys
import time

import httpx

from resident import config, mdns


def pair(host: str, name: str, http_port: int, https_port: int, insecure: bool) -> int:
    addr = mdns.resolve(host)
    plain = f"http://{addr}:{http_port}"
    try:
        if insecure:
            ca_pem, verify, base = "", True, plain
        else:
            r = httpx.get(f"{plain}/ca.crt", timeout=10)
            r.raise_for_status()
            ca_pem = r.text
            fp = config.fingerprint(ca_pem)
            print(f"The server's certificate authority: {fp}")
            print(f"  Compare it with the one on http://{host}:{http_port}/ca before you type the code.")
            verify, base = config.tls_context(ca_pem), f"https://{addr}:{https_port}"
        with httpx.Client(verify=verify, timeout=10) as c:
            started = c.post(f"{base}/resident/pair", json={"name": name}).raise_for_status().json()
            code = started["code"]
            print(f"\nPairing code: {code[:3]} {code[3:]}")
            print("Open Cantinero's web chat → Settings → Residents, type the code and press Pair.")
            print(f"Waiting (the code lasts {started.get('expires_in', 600) // 60} minutes)…")
            deadline = time.monotonic() + started.get("expires_in", 600)
            while time.monotonic() < deadline:
                time.sleep(2)
                r = c.get(f"{base}/resident/pair/{started['pairing_id']}")
                if r.status_code == 404:
                    print("The code expired. Run this again for a new one.", file=sys.stderr)
                    return 1
                got = r.raise_for_status().json()
                if got.get("status") == "paired":
                    config.save({"host": host, "http_port": http_port, "https_port": https_port,
                                 "insecure_http": insecure, "ca_pem": ca_pem,
                                 "resident_id": got["resident_id"], "token": got["token"], "name": name})
                    print(f"Paired as {name!r}. Start it with: python -m resident serve")
                    return 0
    except httpx.HTTPError as e:
        print(f"Couldn't reach Cantinero at {host} ({addr}): {e}", file=sys.stderr)
        return 1
    print("The code expired. Run this again for a new one.", file=sys.stderr)
    return 1


def status() -> int:
    cfg = config.load()
    if not config.paired(cfg):
        print("Not paired. Run: python -m resident pair <server>")
        return 1
    base = (f"http://{mdns.resolve(cfg['host'])}:{cfg.get('http_port', 7867)}" if cfg.get("insecure_http")
            else f"https://{mdns.resolve(cfg['host'])}:{cfg.get('https_port', 7868)}")
    verify = True if cfg.get("insecure_http") else config.tls_context(cfg["ca_pem"])
    try:
        r = httpx.get(f"{base}/resident/self", headers={"Authorization": f"Bearer {cfg['token']}"},
                      verify=verify, timeout=10)
    except httpx.HTTPError as e:
        print(f"Paired with {cfg['host']}, but it doesn't answer: {e}")
        return 1
    if r.status_code == 401:
        print(f"{cfg['host']} no longer knows this resident: pair it again.")
        return 1
    me = r.json()
    print(f"Paired with {cfg['host']} as {me['resident']['name']!r}, for {me['account']}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m resident", description="Cantinero's resident.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="pair with a Cantinero server")
    p.add_argument("host", help="the server, e.g. amd-halo.local")
    p.add_argument("--name", default=socket.gethostname().split(".")[0].capitalize())
    p.add_argument("--http-port", type=int, default=7867)
    p.add_argument("--https-port", type=int, default=7868)
    p.add_argument("--insecure-http", action="store_true",
                   help="talk to the server over plain HTTP (only if it has no HTTPS port)")
    sub.add_parser("status", help="is it paired, and does the server answer?")
    sub.add_parser("serve", help="run the resident service")
    a = ap.parse_args(argv)
    if a.cmd == "pair":
        return pair(a.host, a.name, a.http_port, a.https_port, a.insecure_http)
    if a.cmd == "status":
        return status()
    from resident.service import run
    run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
