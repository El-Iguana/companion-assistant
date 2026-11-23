"""The resident's own state: which server, its token, the server's CA (pinned at pairing).

One JSON file, mode 0600, in ``$RESIDENT_DATA`` (default ``~/.local/share/cantinero-resident``;
the container mounts it at ``/resident``). Never the token in a log line.
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
from pathlib import Path


def data_dir() -> Path:
    d = Path(os.environ.get("RESIDENT_DATA") or Path.home() / ".local" / "share" / "cantinero-resident")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return data_dir() / "resident.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def paired(cfg: dict) -> bool:
    return bool(cfg.get("token") and cfg.get("host"))


def fingerprint(pem: str) -> str:
    """SHA-256 of the certificate's DER, as the server's /ca page shows it (AA:BB:…)."""
    der = ssl.PEM_cert_to_DER_cert(pem)
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def tls_context(ca_pem: str) -> ssl.SSLContext:
    """Trust exactly the server's own CA. The host name is not checked: the resident may
    reach the server by an address the certificate doesn't name (a .local name resolved
    here to an IP), and only a certificate signed by this pinned, name-constrained CA
    passes, which no other machine has."""
    ctx = ssl.create_default_context(cadata=ca_pem)
    ctx.check_hostname = False
    return ctx
