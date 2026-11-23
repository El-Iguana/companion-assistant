"""The resident's own routes (RESIDENT_PLAN.md R-1, R-2). Everything else a resident calls
is the desktop page's routes (``/pdesk/*``, a card's PNG and ``avatar.vrm``,
``/pmedia/music/duck``), which take its bearer token where they say ``resident=True``.

| route | what |
|---|---|
| ``POST /resident/pair`` | public: ``{name}`` -> ``{pairing_id, code, expires_in}`` |
| ``GET /resident/pair/{pairing_id}`` | public: pending, or the token, once |
| ``GET /resident/self`` | bearer: this resident and its account |
| ``PUT /resident/self`` | bearer: ``{orpheus_player_id, orpheus_player_name}`` (its built-in player) |
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from pantheon_web.media import _ctx

router = APIRouter(prefix="/resident")


@router.post("/pair")
async def pair(request: Request):
    from services import residents

    body = await request.json() if (request.headers.get("content-type") or "").startswith("application/json") else {}
    addr = request.client.host if request.client else ""
    try:
        return residents.start_pairing(str((body or {}).get("name") or ""), addr)
    except residents.PairingRefused as e:
        raise HTTPException(status_code=429, detail=str(e)) from None


@router.get("/pair/{pairing_id}")
async def pair_poll(pairing_id: str):
    from services import residents

    got = residents.poll_pairing(pairing_id)
    if got is None:
        raise HTTPException(status_code=404, detail="no such pairing request (expired, or already paired)")
    return got


@router.get("/self")
async def whoami(request: Request):
    from services import residents

    cur = _ctx(request, resident=True)
    rec = getattr(request.state, "resident", None)
    if rec is None:
        raise HTTPException(status_code=401, detail="residents only")
    from services.skills import orpheus_media as om

    # Where the account's music server is, so the resident can pair its player with it.
    music = om.load(cur.cards.parent)
    return {"resident": residents.public(rec), "account": cur.user.username,
            "orpheus_url": music.get("url", "") if om.paired(music) else ""}


@router.put("/self")
async def register_player(request: Request):
    from services import residents

    cur = _ctx(request, resident=True)
    rec = getattr(request.state, "resident", None)
    if rec is None:
        raise HTTPException(status_code=401, detail="residents only")
    body = await request.json() or {}
    out = residents.update(cur.user.id, rec["id"],
                           orpheus_player_id=str(body.get("orpheus_player_id") or ""),
                           orpheus_player_name=str(body.get("orpheus_player_name") or ""))
    return {"resident": out}
