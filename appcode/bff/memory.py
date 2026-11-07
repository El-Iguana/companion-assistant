"""BFF: what a chat remembers — the chat header's "Memory…".

* **This chat** — pinned facts of the session (always in context).
* **Long-term** — per (character, persona): standing instructions, facts and task notes
  (``AssistantMemory``). Entries round-trip **by id**, so editing never relabels an
  auto-written fact as hand-written (``main.put_assistant_memory`` relies on it).
* Long-term is **only written back if it was loaded** — a failed read must not let an
  empty form overwrite real remembered instructions.

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
        out = {"ok": True, "session": s.memory.model_dump(), "long": None,
               "character": card.name if card else ""}
        if card is not None:
            out["long"] = await main.get_assistant_memory(card.id, pid, cur=cur)
        return out

    async def save(self, session_id: str, pinned: list, long: dict | None = None) -> dict:
        """Write this chat's pinned facts, and long-term only when ``long`` is given (it is
        only sent when the page loaded it).

        ``long`` must be annotated as optional: pytincture checks every argument against
        its annotation before the call, and ``None`` is not a ``dict``. Declared as
        ``long: dict = None``, every save of a chat without long-term memory was
        answered 400 and nothing was written."""
        import main
        from fastapi import HTTPException

        scope, err = self._scope(session_id)
        if err:
            return err
        cur, s, card, pid = scope
        try:
            await main.set_memory(s.id, {"pinned": list(pinned or [])}, cur=cur)
            if long is not None and card is not None:
                body = {"character_id": card.id, "persona_id": pid,
                        "directives": long.get("directives") or [], "facts": long.get("facts") or [],
                        "notes": [ln.strip() for ln in str(long.get("notes") or "").split("\n")
                                  if ln.strip()]}
                await main.put_assistant_memory(body, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True}
