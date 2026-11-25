"""Plain GET routes for what the page loads by URL: card and persona portraits, card
export and import, VRM models, speech in and out.

Why not BFF: the BFF is JSON (8 MiB requests), and an image belongs in an ``<img src>``
the browser can cache; uploads are multipart files (a VRM model is tens of MB). These routes read the signed-in user from
pytincture's session (``request.session["user"]``, the claims ``pantheon_web.auth``
returned at login) and resolve them to that user's library, so a URL only ever serves
the caller's own files — the same isolation the old routes get from Pantheon's cookie.

Caching: a strong ETag from the card/persona file's mtime. A portrait changes only when
its JSON is rewritten, so revalidation is a stat and a 304.
"""
from __future__ import annotations

import base64
import re

import hmac

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

router = APIRouter(prefix="/pmedia")

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _ctx(request: Request, resident: bool = False):
    """The caller's ``main.Ctx``: the signed-in session, or, where ``resident`` allows it,
    a paired resident's bearer token acting for its account (RESIDENT_PLAN.md R-2: the
    desktop page's routes only). The resident is left on ``request.state.resident``."""
    from pantheon_web.auth import ctx_for

    token = _bearer(request)
    if token:
        from services import residents

        rec = residents.authenticate(token) if resident else None
        cur = ctx_for({"user_id": rec["user_id"]}) if rec else None
        if cur is None:
            raise HTTPException(status_code=401, detail="unknown or revoked resident")
        request.state.resident = rec
        return cur
    session = getattr(request, "session", None) or {}
    user = session.get("user")
    cur = ctx_for(user if isinstance(user, dict) else None)
    if cur is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return cur


def _png(request: Request, data_b64: str | None, etag: str) -> Response:
    if not data_b64:
        raise HTTPException(status_code=404, detail="no image")
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    try:
        body = base64.b64decode(data_b64)
    except ValueError:
        raise HTTPException(status_code=404, detail="unreadable image")
    return Response(content=body, media_type="image/png", headers=headers)


def _etag(path, extra: str = "") -> str:
    try:
        return f'"{path.stat().st_mtime_ns:x}{extra}"'
    except OSError:
        return '"0"'


@router.get("/card/{card_id}.png")
def card_portrait(card_id: str, request: Request, upload: int = 0):
    import main
    from models.schema import CharacterCard

    if not _ID.match(card_id):
        raise HTTPException(status_code=404, detail="no such card")
    cur = _ctx(request, resident=True)
    card = main._load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(status_code=404, detail="no such card")
    # `upload=1`: the uploaded portrait itself, whatever the profile picture is set to
    # (the Studio shows what "sprites from the portrait" will start from).
    if upload:
        if not card.image:
            raise HTTPException(status_code=404, detail="no uploaded portrait")
        return _png(request, card.image, _etag(cur.cards / f"{card_id}.json", "u"))
    # Whatever the card's "Profile picture" setting picks (pantheon_web.present).
    from pantheon_web.present import portrait_b64

    return _png(request, portrait_b64(card), _etag(cur.cards / f"{card_id}.json"))


@router.get("/persona/{persona_id}.png")
def persona_portrait(persona_id: str, request: Request):
    import main
    from models.schema import Persona

    if not _ID.match(persona_id):
        raise HTTPException(status_code=404, detail="no such persona")
    cur = _ctx(request)
    persona = main._load(cur.personas, persona_id, Persona)
    if not persona:
        raise HTTPException(status_code=404, detail="no such persona")
    return _png(request, persona.image, _etag(cur.personas / f"{persona_id}.json"))


@router.get("/card/{card_id}/export.png")
async def card_export(card_id: str, request: Request, version: int = 2):
    """The character as a V2/V3 PNG card (download) — ``main.export_card`` unchanged."""
    import main

    if not _ID.match(card_id) or version not in (2, 3):
        raise HTTPException(status_code=404, detail="no such card")
    return await main.export_card(card_id, version, cur=_ctx(request))


# --- uploads + the voice test (Studio, Phase 4) ------------------------------------
def _require_csrf(request: Request) -> None:
    """These POSTs never pass through pytincture's BFF handler, which is what checks
    CSRF there; mirror it. The page sends the token from its CSRF cookie. A resident's
    bearer call needs none: a browser never attaches that header by itself."""
    if getattr(request.state, "resident", None):
        return
    session = getattr(request, "session", None) or {}
    expected = str(session.get("csrf_token") or "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="CSRF validation failed")


@router.post("/import")
async def import_card(request: Request, file: UploadFile = File(...)):
    """A V2/V3 PNG or JSON card -> a new character (``main.import_card`` unchanged)."""
    import main

    cur = _ctx(request)
    _require_csrf(request)
    card = await main.import_card(file, cur=cur)
    return {"ok": True, "id": card["id"], "name": card["name"]}


@router.post("/vrm")
async def upload_vrm(request: Request, file: UploadFile = File(...)):
    """Import a .vrm (``main.upload_vrm``: audited before it is written, never replaces)."""
    import main

    _ctx(request)
    _require_csrf(request)
    return await main.upload_vrm(file, None)


@router.get("/tts-sample.wav")
async def tts_sample(request: Request, voice: str = "", speed: float = 1.0):
    """The Studio's "Test voice": one line in the chosen voice and pace."""
    import main

    _ctx(request)
    speed = max(0.5, min(1.5, float(speed or 1.0)))
    return await main.tts_synth({"text": "Hi there. This is how I sound to you.",
                                 "voice": voice or "af_sarah", "speed": speed}, None)


# --- voice + avatar (Phase 6) ---------------------------------------------------------
@router.post("/tts")
async def tts(request: Request):
    """One sentence of a reply as WAV (``main.tts_synth``). JSON {text, voice, speed}."""
    import main

    _ctx(request)
    _require_csrf(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="bad body")
    text = str(body.get("text") or "")[:2000]
    speed = max(0.5, min(1.5, float(body.get("speed") or 1.0)))
    return await main.tts_synth({"text": text, "voice": str(body.get("voice") or "af_sarah"),
                                 "speed": speed}, None)


@router.post("/stt")
async def stt(request: Request, file: UploadFile = File(...), strict: str = Form(""),
              wake_word: str = Form("")):
    """A recorded utterance (WAV) -> {text} (``main.stt_transcribe``). ``strict=1`` when
    the clip overlapped the character speaking (voice.js herTurn). ``wake_word`` is named to
    Whisper, so "Mira" isn't written "Mirror" (stt.transcribe)."""
    import main

    _ctx(request)
    _require_csrf(request)
    return await main.stt_transcribe(file, None, strict=strict == "1", wake_word=wake_word)


@router.get("/card/{card_id}/avatar.vrm")
async def avatar(card_id: str, request: Request):
    """This card's 3D model (``main.get_avatar``: filename only, never outside data/vrm)."""
    import main

    if not _ID.match(card_id):
        raise HTTPException(status_code=404, detail="no such card")
    return await main.get_avatar(card_id, cur=_ctx(request, resident=True))


@router.get("/vrm")
async def vrm_list(request: Request):
    """Models in data/vrm with their audits (the avatar harness, static/avatar-test.html)."""
    import main

    _ctx(request)
    return await main.list_vrm(None)


@router.get("/vrm/{name}")
async def vrm_file(name: str, request: Request):
    """One model by name, for previewing before it is assigned (``main.get_vrm``)."""
    import main

    _ctx(request)
    return await main.get_vrm(name, None)


@router.get("/dance")
async def dance_list(request: Request):
    """The dance clips she can dance with (#120; services/dance.py): name, tempo, energy."""
    from services import dance

    _ctx(request, resident=True)
    return {"clips": dance.clips()}


@router.get("/dance/{clip_id}.json")
async def dance_clip(clip_id: str, request: Request):
    """One clip (cantinero-dance/1). Its name changes when it does, so it may be cached."""
    from fastapi.responses import FileResponse

    from services import dance

    _ctx(request, resident=True)
    p = dance.path_of(clip_id)
    if p is None:
        raise HTTPException(status_code=404, detail="no such dance clip")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.post("/dance/{clip_id}/tempo")
async def dance_tempo(clip_id: str, request: Request):
    """Her Settings' tempo slider (#132): ``{"bpm": 104}`` sets this clip's tempo for good."""
    from services import dance

    _ctx(request, resident=True)
    _require_csrf(request)
    body = await request.json()
    try:
        bpm = float((body or {}).get("bpm"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bpm required") from None
    try:
        return {"clip": dance.set_tempo(clip_id, bpm)}
    except KeyError:
        raise HTTPException(status_code=404, detail="no such dance clip") from None


@router.get("/help.html")
def help_page(request: Request):
    """The in-app guide (pantheon_web/assets/help.html)."""
    from pathlib import Path

    _ctx(request)
    raw = (Path(__file__).resolve().parent / "assets" / "help.html").read_text(encoding="utf-8")
    return Response(content=raw, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


# --- the DJ: each music track's tempo (#114) --------------------------------------------------
@router.get("/music/tempo")
async def music_tempo(request: Request, ids: str = "", beats: str = ""):
    """The player asks before a track starts: ``?ids=681,682`` -> ``{"tempo": {"681": 129.2}}``.
    Only measured music tracks are in it (spoken word never is); the rest are measured in the
    background, so asking also keeps that going. ``&beats=1``: also every beat's time (s) of
    those tracks, ``{"beats": {"681": [0.35, 0.82, ...]}}`` (#124: dancing on the beat)."""
    from services.skills import music_tempo as mt
    from services.skills import orpheus_media as om

    cur = _ctx(request, resident=True)
    root = cur.cards.parent
    wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()][:500]
    st = om.load(root)
    if om.paired(st):
        mt.refresh_soon(root, st)
    out = {"tempo": {str(k): v for k, v in mt.bpm_of(root, wanted).items()}}
    if beats == "1":
        out["beats"] = {str(k): v for k, v in mt.beats_of(root, wanted[:4]).items()}
    return out


# --- the DJ: music under her voice ----------------------------------------------------
@router.post("/music/duck")
async def music_duck(request: Request):
    """voice.js: she started speaking or listening ({"on": true}), or stopped. Lowers the
    Orpheus player's music while she does (Phase 5 duck/unduck). The device token stays
    here; the page only says when. Answers at once; the command runs in the background."""
    import asyncio

    from services.skills import media as dj

    cur = _ctx(request, resident=True)
    _require_csrf(request)
    body = await request.json()
    on = bool(body.get("on")) if isinstance(body, dict) else False
    # A resident ducks its own player locally and doesn't come here; the web page (testing)
    # ducks the default resident's (R-7).
    if getattr(request.state, "resident", None) is None:
        asyncio.ensure_future(dj.duck(cur.user.id, cur.cards.parent, on))
    return {"ok": True}
