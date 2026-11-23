"""The desktop avatar's page (``/desktop``) and the few JSON routes it runs on.

``desktop/pantheon-desktop.py`` is a borderless, transparent WebKitGTK window pointed at
``/desktop``: her in the corner of the screen, no chat window. The page is plain HTML +
``static/js/desktop.js`` on top of the same two islands the main page uses
(``voice.js`` for speech in and out, ``vtuber.mjs`` for the 3D model), deliberately
**not** the Pyodide app: the window has no chrome and nothing to type into, so a Python
runtime would be startup cost for nothing.

Routes (signed-in user from pytincture's session, as in ``media.py``; POSTs need the CSRF
header, as there):

| route | what |
|---|---|
| ``GET /desktop`` | the page; signed out -> the login page, then back here |
| ``GET /pdesk/state`` | characters, recent chats, voice settings |
| ``POST /pdesk/open`` | ``{character_id}`` -> her latest chat, or a new one; card + session |
| ``POST /pdesk/chat`` | ``{session_id, text}`` -> ``main.chat``'s SSE stream, unchanged |
| ``POST /pdesk/sweep`` | ``{session_id}`` -> the idle fold into long-term memory |
| ``POST /pdesk/settings`` | a settings patch (voice settings, ``desktop_card_id``) |

The tray menu talks to the page, never to these routes: the page holds the session.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pantheon_web.media import _ctx, _require_csrf

router = APIRouter()

# Settings the page may change: the voice island's own, and which character it shows.
_SETTINGS = ("hands_free", "full_duplex", "mic_device_id", "mic_device_label", "desktop_card_id")
_VOICE = ("hands_free", "wake_word", "wake_mode", "sleep_phrase", "farewell", "sleep_after_s",
          "vad_silence_ms", "vad_threshold", "full_duplex", "mic_device_id", "mic_device_label")


def _card_info(card) -> dict:
    from pantheon_web.present import has_portrait

    return {"id": card.id, "name": card.name,
            "updated_at": card.updated_at, "has_portrait": has_portrait(card),
            "expressions": [],                    # portraits only now; see bff/chat.py
            "has_vrm": bool(card.vrm) and card.avatar_mode == "vrm", "vrm": card.vrm,
            "figure": card.figure,
            "tts_voice": card.tts_voice, "tts_speed": card.tts_speed,
            "wake_word": card.wake_word, "sleep_phrase": card.sleep_phrase,
            "farewell": card.farewell}


@router.get("/desktop", include_in_schema=False)
def desktop_page(request: Request):
    from pantheon_web.auth import ctx_for

    user = (getattr(request, "session", None) or {}).get("user")
    if ctx_for(user if isinstance(user, dict) else None) is None:
        # pytincture's login returns to session["return_to"] (a relative path) once
        # signed in, so the shell lands back here rather than on the main page.
        qs = request.url.query
        request.session["return_to"] = "/desktop" + (f"?{qs}" if qs else "")   # keeps ?card=
        return RedirectResponse("/pantheon/login", status_code=302)
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-store"})


@router.get("/pdesk/state")
async def state(request: Request):
    import main
    from models.schema import CharacterCard, ChatSession

    cur = _ctx(request)
    cards = sorted(main._list(cur.cards, CharacterCard), key=lambda c: c.name.lower())
    sessions = sorted(main._list(cur.sessions, ChatSession), key=lambda s: s.updated_at, reverse=True)
    settings = main._load_settings(cur).model_dump()
    return {
        "characters": [{"id": c.id, "name": c.name,
                        "avatar": bool(c.vrm) and c.avatar_mode == "vrm"} for c in cards],
        # Only what picking a chat needs: whose, and how recent.
        "recent": [{"id": s.id, "character_id": s.character_id} for s in sessions[:50]],
        "settings": {k: settings.get(k) for k in _VOICE + ("desktop_card_id",)},
    }


@router.post("/pdesk/open")
async def open_chat(request: Request):
    """Her most recent chat, or a new one. Continuing rather than starting
    cold on every launch: a desktop companion that forgets each boot is worse."""
    import main
    from models.schema import CharacterCard, ChatSession

    cur = _ctx(request)
    _require_csrf(request)
    body = await request.json()
    cid = str((body or {}).get("character_id") or "")
    card = main._load(cur.cards, cid, CharacterCard)
    if card is None:
        raise HTTPException(status_code=404, detail="no such character")
    mine = sorted((s for s in main._list(cur.sessions, ChatSession)
                   if s.character_id == cid),
                  key=lambda s: s.updated_at, reverse=True)
    session = mine[0] if mine else ChatSession(**await main.create_session({"character_id": cid}, cur=cur))
    last = next((m for m in reversed(session.messages) if m.role == "char"), None)
    return {"session_id": session.id, "card": _card_info(card),
            "mood": session.mood.model_dump(),
            "last": last.content if last else ""}


@router.post("/pdesk/chat")
async def chat(request: Request):
    """``main.chat`` unchanged: tokens, ``expr`` beats, then ``done`` (or ``error``)."""
    import main
    from models.schema import ChatRequest

    cur = _ctx(request)
    _require_csrf(request)
    body = await request.json() or {}
    return await main.chat(ChatRequest(session_id=str(body.get("session_id") or ""),
                                       input_text=str(body.get("text") or "")), cur=cur)


@router.post("/pdesk/sweep")
async def sweep(request: Request):
    import main

    cur = _ctx(request)
    _require_csrf(request)
    body = await request.json() or {}
    return await main.sweep_session(str(body.get("session_id") or ""), cur=cur)


@router.post("/pdesk/settings")
async def settings(request: Request):
    import main

    cur = _ctx(request)
    _require_csrf(request)
    body = await request.json() or {}
    patch = {k: v for k, v in body.items() if k in _SETTINGS}
    saved = await main.put_settings(patch, cur=cur)
    return {k: saved.get(k) for k in _VOICE + ("desktop_card_id",)}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cantinero</title>
<link rel="stylesheet" href="/static/css/desktop.css">
</head>
<body>
  <div id="stage">
    <img id="ch-portrait" alt="" hidden>
    <div id="ch-avatar-slot"></div>
  </div>
  <div id="dt-hud">
    <div id="dt-caption"></div>
    <div id="dt-bar">
      <button id="dt-mic" class="dt-btn" title="Hands-free listening" aria-label="Hands-free listening">&#127897;</button>
      <button id="dt-tts" class="dt-btn" title="Voice output" aria-label="Voice output">&#128264;</button>
      <span id="dt-state" class="dt-state"></span>
      <a href="/pantheon/" class="dt-btn" title="Open the full window" target="_blank">&#10530;</a>
    </div>
  </div>
  <script src="/static/js/voice.js"></script>
  <script src="/static/js/desktop.js"></script>
</body>
</html>
"""
