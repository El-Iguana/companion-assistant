"""Authentication primitives — stdlib only (no bcrypt/jwt dependency).

- Passwords: scrypt (memory-hard) with a per-user random salt.
- Sessions: an HMAC-signed, stateless token `uid.exp.sig` carried in an
  HttpOnly cookie. Signed with a server secret persisted under data/auth/.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def new_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT).hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not (salt and expected_hash):
        return False
    try:
        return hmac.compare_digest(hash_password(password, salt), expected_hash)
    except Exception:
        return False


def make_token(secret: bytes, user_id: str, ttl_days: int = 30) -> str:
    exp = int(time.time()) + ttl_days * 86400
    body = f"{user_id}.{exp}"
    sig = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(secret: bytes, token: str) -> str | None:
    """Return the user_id if the token is valid and unexpired, else None."""
    try:
        user_id, exp, sig = token.rsplit(".", 2)   # uuids contain no dots
        body = f"{user_id}.{exp}"
        good = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, sig):
            return None
        if int(exp) < time.time():
            return None
        return user_id
    except Exception:
        return None
