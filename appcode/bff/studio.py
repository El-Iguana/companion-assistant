"""BFF: the character Studio — load a card for editing, save it, duplicate it, and the
expression-sprite and avatar tools.

**Saving takes a whitelist.** The page sends only the fields it edits (``EDITABLE``); they
are applied onto the card as stored and the result validated by Pantheon's own
``CharacterCard`` model, then written with ``main.update_card`` / ``create_card``. So
sprites, the VRM assignment's file, spec fields this form does not show (V3 extras,
``extensions`` beyond Pantheon's starters) and anything a later feature adds survive a
save untouched — the classic editor got the same effect by round-tripping the whole card.

**Sprite generation runs as a job** (``pantheon_web.jobs``): it takes minutes, a BFF call
30 s. ``main.generate_expressions`` / ``regenerate_expression`` do the work unchanged.

Holds nothing between calls.
"""
from __future__ import annotations

import re

from pytincture.dataclass import backend_for_frontend

# Card fields the Studio edits, by name. Anything else on the card is preserved.
EDITABLE = (
    "name", "nickname", "creator", "character_version", "creator_notes",
    "description", "personality", "scenario", "first_mes", "mes_example",
    "alternate_greetings", "system_prompt", "post_history_instructions",
    "tags", "appearance",
    "tts_voice", "tts_speed", "wake_word", "sleep_phrase", "farewell", "vrm", "avatar_mode",
    "portrait_source", "portrait_sprite",
)


# Audio checkpoints in the shared ComfyUI (Nocturne's ACE-Step, Stable Audio).
AUDIO_MODEL = re.compile(r"ace[_-]?step|stable[_-]?audio|audio", re.I)


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
        from pantheon_web import jobs
        from pantheon_web.present import starters
        from services import expressions, tts

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
        data["sprites"] = sorted(card.expressions.keys())
        try:
            checkpoints = await main.comfy.list_checkpoints()
        except Exception:  # noqa: BLE001 - ComfyUI down is a normal state
            checkpoints = []
        # The shared ComfyUI also holds Nocturne's audio models; they can't draw a sprite.
        checkpoints = [c for c in checkpoints if not AUDIO_MODEL.search(c)]
        kontext = bool(checkpoints) and await main.comfy.has_kontext()
        running = jobs.running_for(cur.user.id, card.id) if card_id else None
        return {
            "ok": True, "card": data, "is_new": not card_id,
            "voices": await asyncio.to_thread(tts.voices),
            "vrms": await self._vrms(),
            "checkpoints": checkpoints,
            "kontext": kontext,
            "emotions": {"core": list(expressions.CORE_EMOTIONS), "full": list(expressions.FULL_EMOTIONS)},
            "job": running.public() if running else None,
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

    # --- expression sprites ---------------------------------------------------------
    async def generate(self, card_id: str, params: dict) -> dict:
        """Start a whole-set generation (replaces the card's sprites when it finishes)."""
        return await self._job(card_id, "Generating sprites", "generate_expressions",
                               card_id, dict(params or {}))

    async def regenerate(self, card_id: str, emotion: str, params: dict) -> dict:
        return await self._job(card_id, f"Generating {emotion}", "regenerate_expression",
                               card_id, emotion, dict(params or {}))

    async def _job(self, card_id: str, label: str, handler: str, *args) -> dict:
        import main
        from models.schema import CharacterCard
        from pantheon_web import jobs

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        card = main._load(cur.cards, card_id or "", CharacterCard)
        if card is None:
            return {"ok": False, "error": "save the character first"}
        params = args[-1] or {}
        # Portrait + Kontext needs no model (the handler checks the rest, and says why).
        no_model_needed = (params.get("source") == "portrait" and params.get("method") == "kontext") \
            or (handler == "regenerate_expression" and card.expr_from_upload and card.expr_kontext)
        if not params.get("checkpoint") and not no_model_needed:
            return {"ok": False, "error": "pick a model (ComfyUI must be running)"}
        try:
            job = jobs.start(cur.user.id, card_id, label,
                             lambda: getattr(main, handler)(*args, cur=cur))
        except jobs.Busy as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "job": job.public()}

    async def job(self, job_id: str) -> dict:
        from pantheon_web import jobs

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        job = jobs.get(cur.user.id, job_id or "")
        if job is None:
            return {"ok": False, "error": "that job is gone (the server restarted?)"}
        return {"ok": True, "job": job.public()}

    async def sprites(self, card_id: str) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            r = await main.list_expressions(card_id or "", cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True, "sprites": r["emotions"]}

    async def delete_sprite(self, card_id: str, emotion: str) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            await main.delete_expression(card_id or "", emotion or "", cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}

    # --- avatar models -------------------------------------------------------------
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
