"""Plain GET routes for images the new page shows: card portraits, expression sprites,
persona portraits.

Why not BFF: the BFF is JSON with a 2 MiB response cap, and an image belongs in an
``<img src>`` the browser can cache. These routes read the signed-in user from
pytincture's session (``request.session["user"]``, the claims ``pantheon_web.auth``
returned at login) and resolve them to that user's library, so a URL only ever serves
the caller's own files — the same isolation the old routes get from Pantheon's cookie.

Caching: a strong ETag from the card/persona file's mtime. A portrait changes only when
its JSON is rewritten, so revalidation is a stat and a 304.
"""
from __future__ import annotations

import base64
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

router = APIRouter(prefix="/pmedia")

_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EMOTION = re.compile(r"^[a-z_]{1,32}$")


def _ctx(request: Request):
    from pantheon_web.auth import ctx_for

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
def card_portrait(card_id: str, request: Request):
    import main
    from models.schema import CharacterCard

    if not _ID.match(card_id):
        raise HTTPException(status_code=404, detail="no such card")
    cur = _ctx(request)
    card = main._load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(status_code=404, detail="no such card")
    # Card art, else the neutral sprite — a card made in the studio may have only sprites.
    b64 = card.image or card.expressions.get("neutral")
    return _png(request, b64, _etag(cur.cards / f"{card_id}.json"))


@router.get("/card/{card_id}/expr/{emotion}.png")
def card_expression(card_id: str, emotion: str, request: Request):
    import main
    from models.schema import CharacterCard

    if not _ID.match(card_id) or not _EMOTION.match(emotion):
        raise HTTPException(status_code=404, detail="no such sprite")
    cur = _ctx(request)
    card = main._load(cur.cards, card_id, CharacterCard)
    if not card:
        raise HTTPException(status_code=404, detail="no such card")
    # Same fallback chain as main.get_expression: the emotion, neutral, then the card art.
    b64 = card.expressions.get(emotion) or card.expressions.get("neutral") or card.image
    return _png(request, b64, _etag(cur.cards / f"{card_id}.json", emotion))


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
