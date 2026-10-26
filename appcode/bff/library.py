"""BFF: characters — the profile modal's data, starting a chat, deleting a card.

Holds nothing between calls (pytincture re-executes this module per call). Starting a
chat is ``main.create_session`` unchanged, so greeting choice, persona, scenario presets
and the empty-greeting fallback all behave exactly as on the classic page.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend


@backend_for_frontend
class Library:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    async def profile(self, card_id: str) -> dict:
        """Everything the profile modal shows, for one character."""
        import main
        from pantheon_web.present import has_portrait, starters, tagline
        from models.schema import CharacterCard, ChatSession, Persona, RelationshipPreset
        from services import prompt_builder

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        card = main._load(cur.cards, card_id or "", CharacterCard)
        if card is None:
            return {"ok": False, "error": "that character no longer exists"}
        persona = main._default_persona(cur)

        def sub(text: str) -> str:
            return prompt_builder.substitute_macros(text or "", card.name, persona.name)

        greetings = [sub(g) for g in [card.first_mes, *card.alternate_greetings]]
        chats = sorted((s for s in main._list(cur.sessions, ChatSession)
                        if s.character_id == card.id),
                       key=lambda s: s.updated_at or "", reverse=True)
        roleplay = card.chat_mode != "assistant"
        rel = None
        if roleplay:
            rel = await main.get_relationship(card.id, persona.id, cur=cur)
        # "Continue" means the conversation, not the newest file: a chat that is only the
        # greeting (started and abandoned) must not hide an older real one behind it.
        talked = [s for s in chats if len(s.messages) > 1]
        resume = talked[0] if talked else None
        return {
            "ok": True,
            "card": {
                "id": card.id, "name": card.name, "tagline": tagline(card),
                "description": sub(card.description), "personality": sub(card.personality),
                "scenario": sub(card.scenario), "greetings": greetings,
                "tags": list(card.tags or []), "mode": card.chat_mode,
                "creator": card.creator, "starters": starters(card),
                "has_portrait": has_portrait(card),
                "sprites": len(card.expressions), "has_vrm": bool(card.vrm) and card.avatar_mode == "vrm",
                "updated_at": card.updated_at,
            },
            "chats": {"count": len(chats),
                      "latest_at": chats[0].updated_at if chats else "",
                      "resume_id": resume.id if resume else ""},
            "persona": {"id": persona.id, "name": persona.name},
            "personas": [{"id": p.id, "name": p.name, "title": p.title}
                         for p in main._list(cur.personas, Persona)],
            "presets": [{"id": p.id, "name": p.name, "character_id": p.character_id,
                         "persona_id": p.persona_id, "note": p.scenario_note}
                        for p in main._list(cur.presets, RelationshipPreset)],
            "relationship": rel,
        }

    async def start_chat(self, card_id: str, greeting_index: int = 0, persona_id: str = "",
                         preset_id: str = "", mode: str = "") -> dict:
        """A new chat — ``main.create_session`` with the profile's choices."""
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        body = {"character_id": card_id or "", "greeting_index": int(greeting_index or 0)}
        if persona_id:
            body["persona_id"] = persona_id
        if preset_id:
            body["preset_id"] = preset_id
            body["mode"] = "scenario" if mode == "scenario" else "continue"
        try:
            session = await main.create_session(body, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True, "session_id": session["id"]}

    async def delete_card(self, card_id: str) -> dict:
        """Delete a character. Like the classic page, its chats, relationship and memories
        stay on disk (they are hidden from the rail once their character is gone)."""
        import main
        from models.schema import CharacterCard

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        if main._load(cur.cards, card_id or "", CharacterCard) is None:
            return {"ok": False, "error": "that character no longer exists"}
        await main.delete_card(card_id, cur=cur)
        return {"ok": True}
