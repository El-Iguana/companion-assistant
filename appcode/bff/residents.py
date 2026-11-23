"""BFF: Settings → Residents and Music (RESIDENT_PLAN.md R-1, R-6).

Residents are the Cantinero apps on the machines with the speakers. One asks to pair and
shows a 6-digit code; typing the code here pairs it with this account. Music is which
Orpheus player a chat from the web page plays on.

Holds nothing between calls.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend

SIGNED_OUT = {"ok": False, "error": "not signed in", "signed_out": True}


@backend_for_frontend
class Residents:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    async def listing(self) -> dict:
        from services import residents

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        from services.music_hub import HUB

        return {"ok": True, "residents": [dict(r, online=HUB.online(r["id"])) for r in residents.list_for(cur.user.id)],
                "waiting": len(residents.pending_codes())}

    async def approve(self, code: str) -> dict:
        from services import residents

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        rec = residents.approve(cur.user.id, code or "")
        if rec is None:
            return {"ok": False, "error": "No resident is waiting with that code. Codes last 10 minutes."}
        return {"ok": True, "resident": rec}

    async def rename(self, resident_id: str, name: str) -> dict:
        from services import residents

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        rec = residents.update(cur.user.id, resident_id or "", name=name or "")
        return {"ok": True, "resident": rec} if rec else {"ok": False, "error": "no such resident"}

    async def revoke(self, resident_id: str) -> dict:
        from services import residents

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        return {"ok": residents.revoke(cur.user.id, resident_id or "")}

    async def music(self) -> dict:
        from services.skills import media

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        return {"ok": True, **await media.players(cur.user.id, cur.cards.parent)}

    async def set_default_player(self, player_id: str, name: str = "") -> dict:
        from services.skills import media

        cur = self._cur()
        if cur is None:
            return SIGNED_OUT
        media.set_default_player(cur.cards.parent, player_id or "", name or "")
        return {"ok": True}
