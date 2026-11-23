"""BFF: the character Studio — load a card for editing, save it, duplicate it, and the
avatar tools.

**Saving takes a whitelist.** The page sends only the fields it edits (``EDITABLE``); they
are applied onto the card as stored and the result validated by Pantheon's own
``CharacterCard`` model, then written with ``main.update_card`` / ``create_card``. So
sprites, the VRM assignment's file, spec fields this form does not show (V3 extras,
``extensions`` beyond Pantheon's starters) and anything a later feature adds survive a
save untouched — the classic editor got the same effect by round-tripping the whole card.

Holds nothing between calls.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend

# Card fields the Studio edits, by name. Anything else on the card is preserved.
EDITABLE = (
    "name", "nickname", "creator", "character_version", "creator_notes",
    "description", "personality", "scenario", "first_mes", "mes_example",
    "alternate_greetings", "system_prompt", "post_history_instructions",
    "tags", "appearance",
    "tts_voice", "tts_speed", "wake_word", "sleep_phrase", "farewell", "vrm", "avatar_mode", "figure",
    "tone",
    "portrait_source", "portrait_sprite",
)



@backend_for_frontend
class Studio:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    # --- load / save -----------------------------------------------------------
    async def load(self, card_id: str = "") -> dict:
        """A card for editing (blank for a new one) plus the pickers' options."""
        import asyncio

        import main
        from models.schema import CharacterCard
        from pantheon_web.present import starters
        from services import tts

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        card = main._load(cur.cards, card_id, CharacterCard) if card_id else CharacterCard(name="")
        if card is None:
            return {"ok": False, "error": "that character no longer exists"}
        data = card.model_dump(exclude={"expressions", "image", "vrm_still"})
        data["has_vrm_still"] = bool(card.vrm_still)
        data["starters"] = starters(card)
        data["has_portrait"] = bool(card.image)
        # An older card's sprite set, only so its profile picture can stay "Expression
        # sprite" if it was; nothing makes or edits sprites any more.
        data["sprites"] = sorted(card.expressions.keys())
        return {
            "ok": True, "card": data, "is_new": not card_id,
            "voices": await asyncio.to_thread(tts.voices),
            "vrms": await self._vrms(),
        }

    async def _vrms(self) -> list:
        import main

        try:
            return await main.list_vrm(None)
        except Exception:  # noqa: BLE001
            return []

    async def save(self, card_id: str, fields: dict, portrait: str = "",
                   remove_portrait: bool = False, starters: list | None = None,
                   lorebook: list | None = None, vrm_still: str = "") -> dict:
        """Apply the edited fields onto the stored card (or a new one) and save it.

        ``vrm_still`` is a PNG data URL of the 3D model, rendered in the browser; it is
        recorded against the model file it shows (``vrm_still_of``)."""
        import main
        from models.schema import CharacterBook, CharacterCard, LorebookEntry
        from pydantic import ValidationError

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        base = main._load(cur.cards, card_id, CharacterCard) if card_id else CharacterCard()
        if base is None:
            return {"ok": False, "error": "that character no longer exists"}
        data = base.model_dump()
        for key in EDITABLE:
            if key in (fields or {}):
                data[key] = fields[key]
        if not str(data.get("name") or "").strip():
            return {"ok": False, "error": "a character needs a name", "field": "name"}
        if starters is not None:
            ext = dict(data.get("extensions") or {})
            pantheon = dict(ext.get("pantheon") or {})
            pantheon["starters"] = [str(s).strip() for s in starters if str(s).strip()][:6]
            ext["pantheon"] = pantheon
            data["extensions"] = ext
        if lorebook is not None:
            try:
                entries = [LorebookEntry(**e).model_dump() for e in lorebook
                           if str(e.get("content") or "").strip() or e.get("keys")]
            except (ValidationError, TypeError) as exc:
                return {"ok": False, "error": f"lorebook: {exc}"}
            if entries:
                book = dict(data.get("character_book") or CharacterBook().model_dump())
                book["entries"] = entries
                book["name"] = book.get("name") or data["name"]
                data["character_book"] = book
            else:
                data["character_book"] = None
        if remove_portrait:
            data["image"] = None
        elif portrait:
            try:
                from pantheon_web.present import normalise_portrait

                data["image"] = normalise_portrait(portrait)
            except Exception as exc:  # noqa: BLE001 - an unreadable upload
                return {"ok": False, "error": f"that image could not be read ({exc})", "field": "portrait"}
        if vrm_still:
            try:
                from pantheon_web.present import normalise_portrait

                data["vrm_still"] = normalise_portrait(vrm_still)
                data["vrm_still_of"] = data.get("vrm") or ""
            except Exception as exc:  # noqa: BLE001 - a bad capture
                return {"ok": False, "error": f"the 3D still could not be read ({exc})"}
        if data.get("portrait_source") not in ("", "image", "sprite", "vrm"):
            data["portrait_source"] = ""
        try:
            card = CharacterCard(**data)
        except ValidationError as exc:
            first = exc.errors()[0]
            return {"ok": False, "error": f"{'.'.join(map(str, first['loc']))}: {first['msg']}",
                    "field": str(first["loc"][0])}
        from datetime import datetime

        card.updated_at = datetime.now().isoformat()
        if card_id:
            saved = await main.update_card(card_id, card, cur=cur)
        else:
            saved = await main.create_card(card, cur=cur)
        return {"ok": True, "id": saved["id"], "updated_at": saved.get("updated_at")}

    async def duplicate(self, card_id: str) -> dict:
        """A full copy — sprites, portrait, lorebook — under a new id, named "(copy)"."""
        import main
        from models.schema import CharacterCard

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        card = main._load(cur.cards, card_id or "", CharacterCard)
        if card is None:
            return {"ok": False, "error": "that character no longer exists"}
        data = card.model_dump(exclude={"id", "created_at", "updated_at"})
        data["name"] = f"{card.name} (copy)"
        saved = await main.create_card(CharacterCard(**data), cur=cur)
        return {"ok": True, "id": saved["id"]}

    async def vrms(self) -> dict:
        return {"ok": True, "vrms": await self._vrms()}

    async def delete_vrm(self, name: str) -> dict:
        """Remove a model file — refused while any card still uses it."""
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            await main.delete_vrm(name or "", cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True, "vrms": await self._vrms()}
