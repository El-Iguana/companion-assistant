"""Residents on amd-halo (RESIDENT_PLAN.md R1): pairing, the bearer allowlist, and the DJ
playing on the resident's own player.

    .venv/bin/python -m pytest -q tests/test_residents.py
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import main
from services import residents
from test_chat_stream import cur, session  # noqa: F401 — fixtures


@pytest.fixture(autouse=True)
def fresh():
    residents._pending.clear()
    residents._recent.clear()
    yield
    residents._pending.clear()


@pytest.fixture()
def client():
    from pantheon_web.desktop import router as desktop_router
    from pantheon_web.media import router as media_router
    from pantheon_web.resident_api import router as resident_router

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")
    for r in (resident_router, desktop_router, media_router):
        app.include_router(r)
    return TestClient(app)


def _pair(client, cur, name="Predator"):
    started = client.post("/resident/pair", json={"name": name}).json()
    assert client.get(f"/resident/pair/{started['pairing_id']}").json() == {"status": "pending"}
    assert residents.approve(cur.user.id, "000000" if started["code"] != "000000" else "111111") is None
    rec = residents.approve(cur.user.id, started["code"][:3] + " " + started["code"][3:])  # spaces are fine
    assert rec["name"] == name
    got = client.get(f"/resident/pair/{started['pairing_id']}").json()
    assert got["status"] == "paired" and got["resident_id"] == rec["id"]
    assert client.get(f"/resident/pair/{started['pairing_id']}").status_code == 404   # the token goes out once
    return got["token"], rec


def test_pairing_needs_a_person_and_keeps_only_a_hash(client, cur):
    token, rec = _pair(client, cur)
    stored = (main.DATA / "auth" / "residents" / f"{rec['id']}.json").read_text()
    assert token not in stored and "token_hash" in stored
    assert [r["id"] for r in residents.list_for(cur.user.id)] == [rec["id"]]


def test_pairing_requests_are_rate_limited(client):
    codes = [client.post("/resident/pair", json={"name": "x"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_a_resident_reaches_the_desktop_routes_and_nothing_else(client, cur, session):
    token, rec = _pair(client, cur)
    auth = {"Authorization": f"Bearer {token}"}
    state = client.get("/pdesk/state", headers=auth)
    assert state.status_code == 200 and state.json()["characters"][0]["name"] == "Iris"
    # A POST with no CSRF token: a bearer call needs none.
    opened = client.post("/pdesk/open", json={"character_id": session.character_id}, headers=auth)
    assert opened.status_code == 200 and opened.json()["session_id"] == session.id
    me = client.get("/resident/self", headers=auth).json()
    assert me["account"] == cur.user.username and me["orpheus_url"] == ""     # no music server yet
    # Not on its allowlist: importing cards, uploading models, listing models.
    assert client.post("/pmedia/import", files={"file": ("x.json", b"{}")}, headers=auth).status_code == 401
    assert client.get("/pmedia/vrm", headers=auth).status_code == 401
    # No token, no session: refused.
    assert client.get("/pdesk/state").status_code == 401
    assert client.get("/pdesk/state", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_revoking_cuts_it_off_at_once(client, cur):
    token, rec = _pair(client, cur)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/pdesk/state", headers=auth).status_code == 200
    assert residents.revoke("someone-else", rec["id"]) is False
    assert residents.revoke(cur.user.id, rec["id"])
    assert client.get("/pdesk/state", headers=auth).status_code == 401


def test_a_deleted_account_takes_its_residents(client, cur):
    _, rec = _pair(client, cur)
    residents.forget_user(cur.user.id)
    assert residents.list_for(cur.user.id) == []


def test_chat_from_a_resident_targets_its_player(client, cur, session, monkeypatch):
    token, rec = _pair(client, cur)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.put("/resident/self", json={"orpheus_player_id": "p-predator", "orpheus_player_name": "Predator"},
                      headers=auth).json()["resident"]["orpheus_player_id"] == "p-predator"
    seen = {}

    async def fake_chat(req, cur=None, player_id=""):
        from fastapi.responses import JSONResponse
        seen.update(text=req.input_text, player=player_id)
        return JSONResponse({"ok": True})
    monkeypatch.setattr(main, "chat", fake_chat)
    client.post("/pdesk/chat", json={"session_id": session.id, "text": "play rush"}, headers=auth)
    assert seen == {"text": "play rush", "player": rec["id"]}          # plays on this resident (R-6)

    ducks = []

    async def fake_duck(user_id, root, on):
        ducks.append(on)
    from services.skills import media
    monkeypatch.setattr(media, "duck", fake_duck)
    client.post("/pmedia/music/duck", json={"on": True}, headers=auth)
    assert ducks == []                                                  # a resident ducks locally
    client.cookies.clear()
