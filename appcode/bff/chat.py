"""BFF: one chat — open it, stream replies, swipe, edit, delete, resend, clear, sweep, and
(roleplay) narrator events, Continue and drafts of the user's reply.

Every method is a thin call into Pantheon's own handlers (``main.*``), so generation,
swipes, mood, the relationship, memory folding and the idle sweep behave exactly as on
the classic page.

``send`` is a ``@bff_stream`` async generator relaying ``main.chat``'s SSE: tokens, ``expr``
beats, errors, then the ``done`` event (mood, relationship, milestones). Stopping in the
browser closes this generator, which closes the handler's generator, which stops
KoboldCPP. As on the classic page, a stopped turn is not saved: ``main.chat`` stores the
exchange only once the reply completes.

Holds nothing between calls (pytincture re-executes this module per call).
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend, bff_stream


# AppSettings the voice island reads (static/js/voice.js).
VOICE = ("hands_free", "wake_word", "wake_mode", "sleep_phrase", "farewell", "sleep_after_s",
         "vad_silence_ms", "vad_threshold", "full_duplex", "mic_device_id", "mic_device_label")


@backend_for_frontend
class Chat:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    def _cur(self):
        from pantheon_web.auth import ctx_for

        return ctx_for(self._user)

    async def open(self, session_id: str) -> dict:
        """The session plus everything the chat view shows around it."""
        import main
        from models.schema import CharacterCard, ChatSession, Persona
        from pantheon_web.present import has_portrait, starters, tagline

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        s = main._load(cur.sessions, session_id or "", ChatSession)
        if s is None:
            return {"ok": False, "error": "that chat no longer exists", "gone": True}
        card = main._load(cur.cards, s.character_id, CharacterCard)
        if card is None:
            return {"ok": False, "error": "this chat's character was deleted", "gone": True}
        persona = main._load(cur.personas, s.persona_id, Persona) or main._default_persona(cur)
        assistant = card.chat_mode == "assistant"
        scenario = s.relationship is not None
        rel = None
        memories = 0
        if not assistant:
            if scenario:
                from services import relationship

                rel = relationship.summary(s.relationship)
            else:
                rel = await main.get_relationship(card.id, persona.id, cur=cur)
                memories = len(main._load_character_memory(cur, card.id, persona.id).facts)
        else:
            am = main._load_assistant_memory(cur, card.id, persona.id)
            memories = len(am.facts) + len(am.directives)
        return {
            "ok": True,
            "session": s.model_dump(),
            "card": {
                "id": card.id, "name": card.name, "tagline": tagline(card), "mode": card.chat_mode,
                "has_portrait": has_portrait(card), "updated_at": card.updated_at,
                "expressions": sorted(card.expressions.keys()),
                "has_vrm": bool(card.vrm) and card.avatar_mode == "vrm", "vrm": card.vrm,
                "starters": starters(card),
                # The voice island's per-character knobs (empty = the global setting).
                "tts_voice": card.tts_voice, "tts_speed": card.tts_speed,
                "wake_word": card.wake_word, "sleep_phrase": card.sleep_phrase,
                "farewell": card.farewell,
            },
            "voice": {k: v for k, v in main._load_settings(cur).model_dump().items() if k in VOICE},
            "persona": {"id": persona.id, "name": persona.name,
                        "has_portrait": bool(persona.image)},
            "relationship": rel,
            "scenario": scenario,
            "memories": memories,
        }

    @bff_stream()
    async def send(self, session_id: str, text: str, regenerate: bool = False, cont: bool = False):
        """A chat turn. ``cont``: roleplay Continue — she goes on with no new message."""
        from models.schema import ChatRequest

        async for ev in self._relay("chat", lambda: ChatRequest(
                session_id=session_id, input_text=text or "", regenerate=bool(regenerate),
                continue_turn=bool(cont))):
            yield ev

    @bff_stream()
    async def narrate(self, session_id: str, direction: str = "", replace: bool = False):
        """A neutral narrator event, saved into the chat (roleplay only)."""
        from models.schema import SceneRequest

        async for ev in self._relay("narrate", lambda: SceneRequest(
                session_id=session_id, direction=direction or "", replace=bool(replace))):
            yield ev

    @bff_stream()
    async def draft(self, session_id: str, direction: str = ""):
        """{{user}}'s next message written for the composer; not saved (roleplay only)."""
        from models.schema import SceneRequest

        async for ev in self._relay("draft_reply", lambda: SceneRequest(
                session_id=session_id, direction=direction or "")):
            yield ev

    async def _relay(self, handler: str, make_req):
        """Run an SSE handler in main and yield its events as dicts."""
        import json

        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            yield {"error": "not signed in"}
            return
        try:
            resp = await getattr(main, handler)(make_req(), cur=cur)
        except HTTPException as exc:
            yield {"error": str(exc.detail)}
            return
        body = resp.body_iterator
        buf = ""
        try:
            async for chunk in body:
                buf += chunk if isinstance(chunk, str) else chunk.decode()
                while "\n\n" in buf:
                    event, buf = buf.split("\n\n", 1)
                    for line in event.splitlines():
                        if line.startswith("data: "):
                            yield json.loads(line[6:])
        finally:
            await body.aclose()

    async def swipe(self, session_id: str, index: int) -> dict:
        """Show stored alternate ``index`` of the last reply (reclassifies its emotion)."""
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            return await main.set_swipe(session_id or "", {"swipe_index": int(index)}, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}

    async def edit(self, session_id: str, message_id: str, content: str) -> dict:
        return await self._call("edit_message", session_id, message_id, {"content": content})

    async def delete(self, session_id: str, message_id: str) -> dict:
        return await self._call("delete_message", session_id, message_id)

    async def truncate_after(self, session_id: str, message_id: str) -> dict:
        return await self._call("truncate_after", session_id, message_id)

    async def clear(self, session_id: str) -> dict:
        return await self._call("clear_messages", session_id)

    async def sweep(self, session_id: str) -> dict:
        """The idle trigger: fold a finished chat into long-term memory (idempotent)."""
        return await self._call("sweep_session", session_id)

    async def _call(self, handler: str, *args) -> dict:
        import main
        from fastapi import HTTPException

        cur = self._cur()
        if cur is None:
            return {"ok": False, "error": "not signed in", "signed_out": True}
        try:
            result = await getattr(main, handler)(*args, cur=cur)
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        return {"ok": True, **(result if isinstance(result, dict) else {})}
