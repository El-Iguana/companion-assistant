"""pytincture login against Pantheon's own accounts, and the signed-in user's context.

Server-only. The new page (``appcode/``) never imports this; BFF classes do, inside their
methods.

``authenticate`` runs once, at login: it checks the password with Pantheon's existing scrypt
code (``services.auth``) against ``data/auth/users`` — the same accounts, the same hashes,
so nobody re-registers and ``manage.py reset-password`` keeps working for both pages.
The claims it returns are what every later BFF call sees as ``_user``.

``ctx_for`` turns those claims back into the ``main.Ctx`` that Pantheon's route handlers
take, re-reading the account each time — a deleted user's old session then resolves to
nothing instead of to their (deleted) library.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def authenticate(email: str, password: str, request: Any = None) -> Optional[dict]:
    import main
    from services import auth

    user = main._user_by_name(email or "")
    if user is None:
        # Same work as a real check, so an unknown name and a wrong password take as long.
        auth.verify_password(password or "", "00" * 16, "00" * 32)
        return None
    if not auth.verify_password(password or "", user.salt, user.password_hash):
        return None
    admin = user.role == "admin"
    return {
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "roles": ["admin", "user"] if admin else ["user"],
    }


def ctx_for(claims: Optional[Mapping[str, Any]]):
    """The signed-in user's ``main.Ctx``, or None when there is no (longer a) user."""
    if not claims:
        return None
    import main

    user = main._user_by_id(str(claims.get("user_id") or ""))
    return main._ctx_for(user) if user else None
