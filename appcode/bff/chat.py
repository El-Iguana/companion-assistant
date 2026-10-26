"""BFF: chat, streamed.

``send`` is a ``@bff_stream`` async generator that relays Pantheon's own ``/api/chat``
handler. ``main.chat`` returns a StreamingResponse of ``data: {...}`` lines; this
parses them back into dicts and yields each as a stream item — tokens, ``expr`` beats,
errors, and the final ``done`` event (mood, relationship, milestones) — unchanged.

Closing the stream in the browser closes this generator, which closes the handler's
generator, which stops KoboldCPP's stream: the same abort path the old page uses.
"""
from __future__ import annotations

from pytincture.dataclass import backend_for_frontend, bff_stream


@backend_for_frontend
class Chat:
    def __init__(self, _user: dict = None) -> None:
        self._user = _user or {}

    @bff_stream()
    async def send(self, session_id: str, text: str, regenerate: bool = False):
        import json

        import main
        from fastapi import HTTPException
        from models.schema import ChatRequest
        from pantheon_web.auth import ctx_for

        cur = ctx_for(self._user)
        if cur is None:
            yield {"error": "not signed in"}
            return
        try:
            resp = await main.chat(ChatRequest(session_id=session_id, input_text=text or "",
                                               regenerate=bool(regenerate)), cur=cur)
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
