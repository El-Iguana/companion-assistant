"""BFF: what a chat remembers, and the relationship's context — the chat header's
"Memory…" and "Relationship context…".

Scoping is the classic page's, exactly:

* **This chat** — pinned facts and the rolling summary of the session.
* **Long-term** — per (character, persona), only for chats with a character that are not
  scenario chats (those run on a session-scoped premise the main timeline would
  contradict). Its shape depends on the card: roleplay has remembered facts and a history
  across chats (``CharacterMemory``); assistant has standing instructions, facts and
  task notes (``AssistantMemory``). Entries round-trip **by id**, so editing never
  relabels an auto-written fact as hand-written (the ``put_*`` handlers rely on it).
* Long-term is **only written back if it was loaded** — a failed read must not let an
  empty form overwrite real remembered history (the classic page's rule too).

Holds nothing between calls.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend


@backend_for_frontend
class Memory:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _scope(self, session_id: str):
        """(cur, session, card, persona_id) or an error dict."""
        import main
        from models.schema import CharacterCard, ChatSession
        from pantheon_web.auth import ctx_for

        cur = ctx_for(self._user)
        if cur is None:
            return None, {"ok": False, "error": "not signed in", "signed_out": True}
        s = main._load(cur.sessions, session_id or "", ChatSession)
        if s is None:
            return None, {"ok": False, "error": "that chat no longer exists"}
        card = main._load(cur.cards, s.character_id, CharacterCard) if s.character_id else None
        pid = s.persona_id or main._default_persona(cur).id
        return (cur, s, card, pid), None

    async def open(self, session_id: str) -> dict:
        import main

        scope, err = self._scope(session_id)
        if err:
            return err
        cur, s, card, pid = scope
        out = {"ok": True, "session": s.memory.model_dump(), "long": None, "long_kind": "",
               "character": card.name if card else ""}
        if card is not None and s.relationship is None:
            if card.chat_mode == "assistant":
                out["long"] = await main.get_assistant_memory(card.id, pid, cur=cur)
                out["long_kind"] = "assistant"
            else:
                out["long"] = await main.get_character_memory(card.id, pid, cur=cur)
                out["long_kind"] = "companion"
        return out

    async def save(self, session_id: str, pinned: list, summary: str,
                   long: dict | None = None) -> dict:
        """Write this chat's memory, and long-term only when ``long`` is given (it is only
        sent when the page loaded it).

        ``long`` must be annotated as optional: pytincture checks every argument against
        its annotation before the call, and ``None`` is not a ``dict``. Declared as
        ``long: dict = None``, every save of a chat without long-term memory (scenario
        and character-less chats) was answered 400 and nothing was written."""
        import main
        from fastapi import HTTPException

        scope, err = self._scope(session_id)
        if err:
            return err
        cur, s, card, pid = scope
        try:
            await main.set_memory(s.id, {"pinned": list(pinned or []), "summary": summary or ""}, cur=cur)
            if long is not None and card is not None and s.relationship is None:
                body = {"character_id": card.id, "persona_id": pid}
                if card.chat_mode == "assistant":
                    body.update(directives=long.get("directives") or [], facts=long.get("facts") or [],
                                notes=[ln.strip() for ln in str(long.get("notes") or "").split("\n") if ln.strip()])
                    await main.put_assistant_memory(body, cur=cur)
                else:
                    body.update(facts=long.get("facts") or [], summary=long.get("summary") or "")
                    if "moments" in long:         # ids still listed; only removal is possible
                        body["moments"] = list(long.get("moments") or [])
                    await main.put_character_memory(body, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}

    # --- relationship context --------------------------------------------------------
    async def context(self, session_id: str) -> dict:
        """Power dynamic / social acceptance for this chat's pairing (effective values)."""
        import main

        scope, err = self._scope(session_id)
        if err:
            return err
        cur, s, card, pid = scope
        if card is None or card.chat_mode == "assistant" or s.relationship is not None:
            return {"ok": False, "error": "this chat has no lasting relationship to adjust"}
        r = await main.get_relationship(card.id, pid, cur=cur)
        return {"ok": True, "power_dynamic": r["power_dynamic"], "social_acceptance": r["social_acceptance"],
                "overridden": r["context_overridden"], "card_power": card.power_dynamic,
                "card_social": card.social_acceptance, "character": card.name}

    async def set_context(self, session_id: str, power: float = 0.0, social: float = 50.0,
                          clear: bool = False) -> dict:
        """Override the card's defaults for this relationship only; ``clear`` goes back."""
        import main

        scope, err = self._scope(session_id)
        if err:
            return err
        cur, s, card, pid = scope
        if card is None or card.chat_mode == "assistant" or s.relationship is not None:
            return {"ok": False, "error": "this chat has no lasting relationship to adjust"}
        body = {"character_id": card.id, "persona_id": pid}
        if clear:
            body.update(power_dynamic=None, social_acceptance=None)
        else:
            body.update(power_dynamic=max(-100.0, min(100.0, float(power))),
                        social_acceptance=max(0.0, min(100.0, float(social))))
        await main.set_relationship(body, cur=cur)
        return {"ok": True}
