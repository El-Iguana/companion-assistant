"""Residents: paired Cantinero apps on the machines with the speakers (RESIDENT_PLAN.md R-1).

A resident asks for a pairing code (no account needed), someone signed in approves the
code in Settings → Residents, and the resident collects a token, once. From then on it
calls this server with ``Authorization: Bearer <token>`` and acts as that account, on an
allowlist of routes (the desktop page's). Only a SHA-256 of the token is kept
(``data/auth/residents/<id>.json``). Revoking deletes the file, so the next call fails.

Pairing requests live in memory: a restart forgets them, and a new code is a click away.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PAIR_TTL_S = 600               # a code is good for 10 minutes
PAIR_MAX_PENDING = 20          # unauthenticated requests held at once
PAIR_PER_ADDR_PER_MIN = 5
LAST_SEEN_EVERY_S = 60
NAME_MAX = 60
_ID = re.compile(r"^[a-f0-9]{32}$")

_lock = threading.Lock()
_pending: dict[str, dict] = {}         # pairing_id -> {code, name, addr, expires, resident_id?, token?}
_recent: dict[str, list[float]] = {}   # addr -> request times


def _dir() -> Path:
    import main

    d = Path(main.DATA) / "auth" / "residents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(rec: dict) -> None:
    p = _dir() / f"{rec['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _read(rid: str) -> dict | None:
    if not _ID.match(rid or ""):
        return None
    try:
        return json.loads((_dir() / f"{rid}.json").read_text())
    except (OSError, ValueError):
        return None


def _clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f]", "", str(name or "")).strip()[:NAME_MAX]
    return name or "Resident"


class PairingRefused(Exception):
    pass


# --- pairing -------------------------------------------------------------------------------

def start_pairing(name: str, addr: str = "") -> dict:
    """A resident asks to pair. Returns its secret pairing id (to poll with) and the code a
    person reads off its screen and types into Settings → Residents."""
    now = time.time()
    with _lock:
        for pid in [p for p, r in _pending.items() if r["expires"] < now]:
            _pending.pop(pid)
        hits = [t for t in _recent.get(addr, []) if now - t < 60]
        if len(hits) >= PAIR_PER_ADDR_PER_MIN or len(_pending) >= PAIR_MAX_PENDING:
            raise PairingRefused("too many pairing requests; try again in a minute")
        _recent[addr] = hits + [now]
        codes = {r["code"] for r in _pending.values()}
        code = f"{secrets.randbelow(10**6):06d}"
        while code in codes:
            code = f"{secrets.randbelow(10**6):06d}"
        pid = secrets.token_urlsafe(24)
        _pending[pid] = {"code": code, "name": _clean_name(name), "addr": addr, "expires": now + PAIR_TTL_S}
        return {"pairing_id": pid, "code": code, "expires_in": PAIR_TTL_S}


def poll_pairing(pairing_id: str) -> dict | None:
    """None: no such request (expired, or already collected). Else {"status": "pending"},
    or, once approved, the token — handed out exactly once."""
    with _lock:
        req = _pending.get(pairing_id)
        if not req or req["expires"] < time.time():
            _pending.pop(pairing_id, None)
            return None
        if "token" not in req:
            return {"status": "pending"}
        _pending.pop(pairing_id)
        return {"status": "paired", "resident_id": req["resident_id"], "token": req["token"]}


def pending_codes() -> list[dict]:
    """What's waiting, for Settings (codes and names; the person still types the code)."""
    now = time.time()
    with _lock:
        return [{"name": r["name"], "expires_in": int(r["expires"] - now)}
                for r in _pending.values() if r["expires"] > now and "token" not in r]


def approve(user_id: str, code: str) -> dict | None:
    """Approve the waiting request with this code for this account. None: no such code."""
    code = re.sub(r"\D", "", str(code or ""))
    with _lock:
        hit = next((r for r in _pending.values()
                    if r["code"] == code and r["expires"] > time.time() and "token" not in r), None)
        if hit is None:
            return None
        token = secrets.token_urlsafe(32)
        rec = {"id": uuid.uuid4().hex, "name": hit["name"], "user_id": user_id,
               "token_hash": _hash(token), "created_at": _now(), "last_seen": None,
               "orpheus_player_id": "", "orpheus_player_name": ""}
        _write(rec)
        hit["resident_id"], hit["token"] = rec["id"], token
    return public(rec)


# --- the paired --------------------------------------------------------------------------------

def public(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("id", "name", "created_at", "last_seen",
                                    "orpheus_player_id", "orpheus_player_name")}


def list_for(user_id: str) -> list[dict]:
    out = []
    for p in sorted(_dir().glob("*.json")):
        rec = _read(p.stem)
        if rec and rec.get("user_id") == user_id:
            out.append(public(rec))
    return sorted(out, key=lambda r: r["created_at"] or "")


def authenticate(token: str) -> dict | None:
    """The resident this token belongs to, or None. Touches last_seen at most once a minute."""
    if not token:
        return None
    h = _hash(token)
    for p in _dir().glob("*.json"):
        rec = _read(p.stem)
        if rec and secrets.compare_digest(rec.get("token_hash", ""), h):
            seen = rec.get("last_seen")
            try:
                stale = not seen or time.time() - datetime.fromisoformat(seen).timestamp() > LAST_SEEN_EVERY_S
            except ValueError:
                stale = True
            if stale:
                rec["last_seen"] = _now()
                _write(rec)
            return rec
    return None


def update(user_id: str, rid: str, **fields) -> dict | None:
    rec = _read(rid)
    if not rec or rec.get("user_id") != user_id:
        return None
    if "name" in fields:
        rec["name"] = _clean_name(fields["name"])
    for k in ("orpheus_player_id", "orpheus_player_name"):
        if k in fields:
            rec[k] = str(fields[k] or "")[:100]
    _write(rec)
    return public(rec)


def revoke(user_id: str, rid: str) -> bool:
    rec = _read(rid)
    if not rec or rec.get("user_id") != user_id:
        return False
    (_dir() / f"{rid}.json").unlink(missing_ok=True)
    return True


def forget_user(user_id: str) -> None:
    """An account was deleted: its residents go with it."""
    for p in _dir().glob("*.json"):
        rec = _read(p.stem)
        if rec and rec.get("user_id") == user_id:
            p.unlink(missing_ok=True)
