"""BFF: one chat — open it, stream replies, swipe, edit, delete, resend, clear, sweep.

Every method is a thin call into Pantheon's own handlers (``main.*``), so generation,
swipes, mood, memory folding and the idle sweep behave exactly as on the classic page.

``send`` is a ``@bff_stream`` async generator relaying ``main.chat``'s SSE: tokens, ``expr``
beats, errors, then the ``done`` event (expression, mood). Stopping in the
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
        am = main._load_assistant_memory(cur, card.id, persona.id)
        memories = len(am.facts) + len(am.directives)
        return {
            "ok": True,
            "session": s.model_dump(),
            "card": {
                "id": card.id, "name": card.name, "tagline": tagline(card),
                "has_portrait": has_portrait(card), "updated_at": card.updated_at,
                "expressions": sorted(card.expressions.keys()),
                "has_vrm": bool(card.vrm) and card.avatar_mode == "vrm", "vrm": card.vrm,
                "figure": card.figure,
                "starters": starters(card),
                # The voice island's per-character knobs (empty = the global setting).
                "tts_voice": card.tts_voice, "tts_speed": card.tts_speed,
                "wake_word": card.wake_word, "sleep_phrase": card.sleep_phrase,
                "farewell": card.farewell,
            },
            "voice": {k: v for k, v in main._load_settings(cur).model_dump().items() if k in VOICE},
            "persona": {"id": persona.id, "name": persona.name,
                        "has_portrait": bool(persona.image)},
            "memories": memories,
        }

    @bff_stream()
    async def send(self, session_id: str, text: str, regenerate: bool = False):
        """A chat turn; ``regenerate`` re-rolls the last reply as a new swipe."""
        from models.schema import ChatRequest

        async for ev in self._relay("chat", lambda: ChatRequest(
                session_id=session_id, input_text=text or "", regenerate=bool(regenerate))):
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
