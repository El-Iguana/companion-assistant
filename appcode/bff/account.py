"""BFF: your settings and password, and — for admins — the accounts on this install.

**Admin checks read the account record, not the session.** ``ctx_for`` re-reads the user
on every call, so ``cur.user.role`` is the role *now*: an admin demoted (or deleted) while
signed in loses these calls at once instead of keeping them until the session expires.
The handlers themselves (``main.create_user`` …) still enforce their own rules: no
deleting yourself, no deleting the last admin, passwords of at least 4 characters.

Holds nothing between calls.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend

# AppSettings fields the Settings screen edits. ``put_settings`` ignores unknown keys too.
SETTINGS = (
    "disable_thinking", "context_limit", "summarize_after", "keep_recent", "summary_max_tokens",
    "recall_enabled", "recall_top_k", "auto_facts_enabled", "auto_facts_max_new",
    "wake_word", "wake_mode", "sleep_phrase", "farewell", "sleep_after_s",
    "vad_silence_ms", "vad_threshold", "full_duplex", "desktop_card_id",
    "hands_free", "mic_device_id", "mic_device_label",
)


@backend_for_frontend
class Account:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    async def settings(self) -> dict:
        import main
        from models.schema import CharacterCard

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        from models.schema import CONTEXT_SIZES
        from services import context

        settings = await main.get_settings(cur=cur)
        backend = await main.kobold.context_limit()
        return {"ok": True, "settings": settings,
                "context": {"backend": backend, "sizes": list(CONTEXT_SIZES),
                            "effective": context.effective(settings.get("context_limit") or 0, backend)},
                "me": {"username": cur.user.username, "role": cur.user.role},
                "characters": [{"id": c.id, "name": c.name}
                               for c in sorted(main._list(cur.cards, CharacterCard), key=lambda c: c.name.lower())]}

    async def save_settings(self, values: dict) -> dict:
        import main
        from pydantic import ValidationError

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        body = {k: v for k, v in (values or {}).items() if k in SETTINGS}
        try:
            saved = await main.put_settings(body, cur=cur)
        except ValidationError as exc:
            first = exc.errors()[0]
            return {"ok": False, "error": f"{first['loc'][0]}: {first['msg']}"}
        return {"ok": True, "settings": saved}

    async def change_password(self, current: str, new: str) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            await main.change_my_password({"current": current or "", "password": new or ""}, cur.user)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}

    # --- admin ---------------------------------------------------------------------
    def _admin(self):
        cur = self._cur()
        if cur is None:
            return None, {"ok": False, "error": "not signed in", "signed_out": True}
        if cur.user.role != "admin":
            return None, {"ok": False, "error": "only an admin can manage accounts"}
        return cur, None

    async def users(self) -> dict:
        import main

        cur, err = self._admin()
        if err:
            return err
        return {"ok": True, "me": cur.user.id, "users": await main.list_users(cur.user)}

    async def create_user(self, username: str, password: str, role: str = "user") -> dict:
        return await self._admin_call("create_user", {"username": username or "",
                                                      "password": password or "", "role": role})

    async def delete_user(self, user_id: str) -> dict:
        return await self._admin_call("delete_user", user_id or "")

    async def reset_password(self, user_id: str, password: str) -> dict:
        return await self._admin_call("reset_password", user_id or "", {"password": password or ""})

    async def copy_sources(self) -> dict:
        """What the admin can copy to another user: their own characters, personas, presets."""
        import main
        from models.schema import CharacterCard, Persona, RelationshipPreset

        cur, err = self._admin()
        if err:
            return err

        def rows(folder, cls):
            return [{"id": x.id, "name": x.name}
                    for x in sorted(main._list(folder, cls), key=lambda x: x.name.lower())]

        cards = []
        for c in sorted(main._list(cur.cards, CharacterCard), key=lambda c: c.name.lower()):
            extras = [f"{len(c.expressions)} sprites"] if c.expressions else []
            if c.vrm:
                extras.append("VRM")
            if c.tts_voice:
                extras.append("voice")
            cards.append({"id": c.id, "name": c.name, "extras": " · ".join(extras)})
        return {"ok": True, "character": cards, "persona": rows(cur.personas, Persona),
                "preset": rows(cur.presets, RelationshipPreset)}

    async def copy_to_user(self, user_id: str, items: list) -> dict:
        """Copy the chosen items into ``user_id``'s library (``main.copy_to_user``)."""
        import main
        from fastapi import HTTPException

        cur, err = self._admin()
        if err:
            return err
        try:
            return await main.copy_to_user(user_id or "", {"items": list(items or [])}, cur.user)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}

    async def _admin_call(self, handler: str, *args) -> dict:
        """main's admin handlers take the acting admin as their last argument."""
        import main
        from fastapi import HTTPException

        cur, err = self._admin()
        if err:
            return err
        try:
            await getattr(main, handler)(*args, cur.user)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}
