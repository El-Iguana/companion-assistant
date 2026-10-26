"""BFF: the signed-in user's library — characters, sessions.

pytincture re-executes this module for every call, so it holds nothing. Each method
resolves the caller's ``main.Ctx`` and calls Pantheon's existing route handler with it:
the handlers already take the context as a plain argument, so no logic is duplicated
and the old page and this one cannot drift apart.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend


@backend_for_frontend
class Library:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    async def me(self) -> dict:
        return {"ok": True, "username": self._user.get("username"),
                "role": self._user.get("role")}

    async def cards(self) -> dict:
        import main
        from pantheon_web.auth import ctx_for

        cur = ctx_for(self._user)
        if cur is None:
            return {"ok": False, "error": "not signed in"}
        cards = await main.list_cards(cur=cur)
        # Portraits are base64 and would blow the BFF's response cap with a large library;
        # the page loads them from an image route instead.
        for c in cards:
            c.pop("image", None)
        return {"ok": True, "cards": cards}

    async def new_session(self, character_id: str) -> dict:
        import main
        from fastapi import HTTPException
        from pantheon_web.auth import ctx_for

        cur = ctx_for(self._user)
        if cur is None:
            return {"ok": False, "error": "not signed in"}
        try:
            s = await main.create_session({"character_id": character_id}, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True, "session": {"id": s["id"], "messages": s["messages"]}}
