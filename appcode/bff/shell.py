"""BFF: what the shell (rail + top bar) needs, and the small actions it offers.

``shell()`` is one round trip for the whole rail: the account, personas and which one is
active, recent chats with a preview line, the user's characters, and the theme. The
preview/persona/character joins are presentation, done here from the same JSON files
``main``'s handlers read — ``main.list_sessions`` stays exactly as the old page uses it.

Holds nothing between calls (pytincture re-executes this module per call).
"""
from __future__ import annotations

import re

from pytincture.dataclass import backend_for_frontend

THEMES = ("bone", "ember", "moss", "dusk", "harbor")


_VOICE_TAG = re.compile(r"<(?:laugh|chuckle|sigh|gasp|cough|sniffle|groan|yawn)>")


def _preview(session) -> str:
    for m in reversed(session.messages):
        text = " ".join(_VOICE_TAG.sub(" ", m.content or "").split())
        if text:
            return text[:80]
    return ""


@backend_for_frontend
class Shell:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    async def shell(self) -> dict:
        import main
        from models.schema import CharacterCard, ChatSession, Persona
        from pantheon_web.present import has_portrait, tagline

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        cards = main._list(cur.cards, CharacterCard)
        personas = main._list(cur.personas, Persona)
        active = main._default_persona(cur)
        sessions = main._list(cur.sessions, ChatSession)
        by_card = {c.id: c for c in cards}
        by_persona = {p.id: p for p in personas}
        chats_per_card: dict[str, int] = {}
        latest_per_card: dict[str, str] = {}
        talked_per_card: dict[str, str] = {}
        for s in sorted(sessions, key=lambda s: s.updated_at or ""):
            chats_per_card[s.character_id] = chats_per_card.get(s.character_id, 0) + 1
            latest_per_card[s.character_id] = s.id          # last write wins = newest
            if len(s.messages) > 1:
                talked_per_card[s.character_id] = s.id
        # Resume the latest real conversation; a greeting-only chat only when there is no
        # other (so the card's pill never piles up new empty chats).
        resume_per_card = {cid: talked_per_card.get(cid) or sid for cid, sid in latest_per_card.items()}
        recent = []
        for s in sorted(sessions, key=lambda s: s.updated_at or "", reverse=True)[:30]:
            card = by_card.get(s.character_id)
            if card is None:
                continue                      # its character was deleted
            persona = by_persona.get(s.persona_id)
            recent.append({
                "id": s.id, "character_id": card.id, "character": card.name,
                "has_portrait": has_portrait(card),
                "persona": persona.name if persona else "",
                "preview": _preview(s), "updated_at": s.updated_at,
                "messages": len(s.messages),
            })
        characters = [{
            "id": c.id, "name": c.name, "tagline": tagline(c), "tags": list(c.tags or [])[:6],
            "has_portrait": has_portrait(c),
            "chats": chats_per_card.get(c.id, 0), "latest_chat": resume_per_card.get(c.id, ""),
            "mode": c.chat_mode,
            "updated_at": c.updated_at,
        } for c in sorted(cards, key=lambda c: c.name.lower())]
        settings = main._load_settings(cur)
        return {
            "ok": True,
            "me": {"username": cur.user.username, "role": cur.user.role},
            "personas": [{"id": p.id, "name": p.name, "title": p.title,
                          "has_portrait": bool(p.image)} for p in personas],
            "persona": {"id": active.id, "name": active.name, "title": active.title,
                        "has_portrait": bool(active.image)},
            "recent": recent,
            "characters": characters,
            "theme": settings.ui_theme if settings.ui_theme in THEMES else "bone",
        }

    async def all_chats(self) -> dict:
        """Every chat, newest first, with the same preview fields as the rail's 30."""
        import main
        from models.schema import CharacterCard, ChatSession, Persona
        from pantheon_web.present import has_portrait

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        cards = {c.id: c for c in main._list(cur.cards, CharacterCard)}
        personas = {p.id: p for p in main._list(cur.personas, Persona)}
        out = []
        for s in sorted(main._list(cur.sessions, ChatSession), key=lambda s: s.updated_at or "",
                        reverse=True):
            card = cards.get(s.character_id)
            if card is None:
                continue
            persona = personas.get(s.persona_id)
            out.append({
                "id": s.id, "character_id": card.id, "character": card.name,
                "has_portrait": has_portrait(card), "persona": persona.name if persona else "",
                "preview": _preview(s), "updated_at": s.updated_at, "created_at": s.created_at,
                "messages": len(s.messages), "scenario": s.relationship is not None,
            })
        return {"ok": True, "chats": out}

    async def status(self) -> dict:
        """KoboldCPP and ComfyUI, probed in parallel (each has a 5 s timeout)."""
        import asyncio

        import main

        kobold, comfy = await asyncio.gather(main.kobold.check_status(),
                                             main.comfy.check_status())
        return {"ok": True, "kobold": kobold, "comfy": comfy}

    async def set_theme(self, theme: str) -> dict:
        import main

        if theme not in THEMES:
            return {"ok": False, "error": f"unknown theme: {theme!r}"}
        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in"}
        await main.put_settings({"ui_theme": theme}, cur=cur)
        return {"ok": True, "theme": theme}

    async def use_persona(self, persona_id: str) -> dict:
        """Make one persona the active ({{user}}) one — the same single-default rule the
        old page's persona editor enforces."""
        import main
        from models.schema import Persona

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in"}
        persona = main._load(cur.personas, persona_id or "", Persona)
        if persona is None:
            return {"ok": False, "error": "no such persona"}
        persona.is_default = True
        main._save(persona, cur.personas)
        main._clear_other_default_personas(cur, persona.id)
        return {"ok": True}

    async def delete_chat(self, session_id: str) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in"}
        try:
            await main.delete_session(session_id or "", cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}
