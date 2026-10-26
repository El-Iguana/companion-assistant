"""BFF: personas (who you are in a chat) and relationship presets (scenario seeds).

Thin calls into ``main``'s handlers; validation is Pantheon's own models. Holds nothing.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend

PERSONA_FIELDS = ("name", "title", "description", "background", "is_default")
PRESET_FIELDS = ("name", "description", "character_id", "persona_id", "scenario_note",
                 "power_dynamic", "social_acceptance", "affection", "trust", "attraction",
                 "tension", "emotional_intimacy", "physical_willingness")


@backend_for_frontend
class People:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    # --- personas ----------------------------------------------------------------
    async def personas(self) -> dict:
        import main
        from models.schema import ChatSession, Persona

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        active = main._default_persona(cur)
        chats: dict[str, int] = {}
        for s in main._list(cur.sessions, ChatSession):
            chats[s.persona_id] = chats.get(s.persona_id, 0) + 1
        return {"ok": True, "active": active.id, "personas": [
            {**p.model_dump(exclude={"image"}), "has_portrait": bool(p.image), "chats": chats.get(p.id, 0)}
            for p in sorted(main._list(cur.personas, Persona), key=lambda p: p.name.lower())]}

    async def save_persona(self, persona_id: str, fields: dict, portrait: str = "",
                           remove_portrait: bool = False) -> dict:
        import main
        from models.schema import Persona
        from pydantic import ValidationError

        from pantheon_web.present import normalise_portrait

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        base = main._load(cur.personas, persona_id, Persona) if persona_id else Persona()
        if base is None:
            return {"ok": False, "error": "that persona no longer exists"}
        data = base.model_dump()
        for key in PERSONA_FIELDS:
            if key in (fields or {}):
                data[key] = fields[key]
        if not str(data.get("name") or "").strip():
            return {"ok": False, "error": "a persona needs a name", "field": "name"}
        if remove_portrait:
            data["image"] = None
        elif portrait:
            try:
                data["image"] = normalise_portrait(portrait)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"that image could not be read ({exc})"}
        try:
            persona = Persona(**data)
        except ValidationError as exc:
            return {"ok": False, "error": str(exc.errors()[0]["msg"])}
        if persona_id:
            saved = await main.update_persona(persona_id, persona, cur=cur)
        else:
            saved = await main.create_persona(persona, cur=cur)
        return {"ok": True, "id": saved["id"]}

    async def delete_persona(self, persona_id: str) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            await main.delete_persona(persona_id or "", cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}

    # --- presets -----------------------------------------------------------------
    async def presets(self) -> dict:
        import main
        from models.schema import CharacterCard, Persona, RelationshipPreset

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        return {"ok": True,
                "presets": [p.model_dump() for p in sorted(main._list(cur.presets, RelationshipPreset),
                                                           key=lambda p: p.name.lower())],
                "characters": [{"id": c.id, "name": c.name}
                               for c in sorted(main._list(cur.cards, CharacterCard), key=lambda c: c.name.lower())],
                "personas": [{"id": p.id, "name": p.name}
                             for p in sorted(main._list(cur.personas, Persona), key=lambda p: p.name.lower())]}

    async def save_preset(self, preset_id: str, fields: dict) -> dict:
        import main
        from models.schema import RelationshipPreset
        from pydantic import ValidationError

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        base = main._load(cur.presets, preset_id, RelationshipPreset) if preset_id else RelationshipPreset()
        if base is None:
            return {"ok": False, "error": "that preset no longer exists"}
        data = base.model_dump()
        for key in PRESET_FIELDS:
            if key in (fields or {}):
                data[key] = fields[key]
        if not str(data.get("name") or "").strip():
            return {"ok": False, "error": "a preset needs a name", "field": "name"}
        try:
            preset = RelationshipPreset(**data)
        except ValidationError as exc:
            return {"ok": False, "error": str(exc.errors()[0]["msg"])}
        from datetime import datetime

        preset.updated_at = datetime.now().isoformat()
        if preset_id:
            saved = await main.update_preset(preset_id, preset, cur=cur)
        else:
            saved = await main.create_preset(preset, cur=cur)
        return {"ok": True, "id": saved["id"]}

    async def delete_preset(self, preset_id: str) -> dict:
        import main

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        await main.delete_preset(preset_id or "", cur=cur)
        return {"ok": True}
