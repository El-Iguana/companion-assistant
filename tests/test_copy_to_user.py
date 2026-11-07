"""Admin: copy characters and personas into another user's library
(``main.copy_to_user``), and the VRM delete guard that now checks every library.

    .venv/bin/python -m pytest -q tests/test_copy_to_user.py

Runs against a throwaway library (PANTHEON_DATA_DIR is set before ``main`` is imported).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PANTHEON_DATA_DIR", tempfile.mkdtemp(prefix="pantheon-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402
from models.schema import CharacterCard, Persona, User  # noqa: E402
from services import auth  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def _user(role: str) -> User:
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role=role, salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return u


@pytest.fixture()
def admin():
    return _user("admin")


@pytest.fixture()
def other():
    return _user("user")


def _card(admin: User, **kw) -> CharacterCard:
    card = CharacterCard(name="Iris", image="UE5H", expressions={"joy": "AAA", "sad": "BBB"},
                         tts_voice="af_heart", tts_speed=1.2, vrm="Butler2.vrm", avatar_mode="vrm",
                         vrm_still="Q1E=", vrm_still_of="Butler2.vrm", portrait_source="vrm", **kw)
    main._save(card, main._ctx_for(admin).cards)
    return card


def test_character_copy_keeps_everything_under_a_new_id(admin, other):
    card = _card(admin)
    r = run(main.copy_to_user(other.id, {"items": [{"kind": "character", "id": card.id}]}, admin))
    assert r["ok"] and len(r["copied"]) == 1
    new_id = r["copied"][0]["id"]
    assert new_id != card.id
    copy = main._load(main._ctx_for(other).cards, new_id, CharacterCard)
    same = ("image", "expressions", "tts_voice", "tts_speed", "vrm", "avatar_mode",
            "vrm_still", "vrm_still_of", "portrait_source", "name")
    assert {f: getattr(copy, f) for f in same} == {f: getattr(card, f) for f in same}
    # the original is untouched and still in the admin's library
    assert main._load(main._ctx_for(admin).cards, card.id, CharacterCard) is not None


def test_persona_copy_is_never_the_targets_default(admin, other):
    p = Persona(name="Captain", image="UE5H", is_default=True)
    main._save(p, main._ctx_for(admin).personas)
    r = run(main.copy_to_user(other.id, {"items": [{"kind": "persona", "id": p.id}]}, admin))
    copy = main._load(main._ctx_for(other).personas, r["copied"][0]["id"], Persona)
    assert copy.name == "Captain" and copy.image == "UE5H" and copy.is_default is False


def test_presets_are_not_a_copy_kind(admin, other):
    with pytest.raises(HTTPException) as e:
        run(main.copy_to_user(other.id, {"items": [{"kind": "preset", "id": "x"}]}, admin))
    assert e.value.status_code == 400


def test_refusals(admin, other):
    card = _card(admin)
    item = {"items": [{"kind": "character", "id": card.id}]}
    with pytest.raises(HTTPException) as e:
        run(main.copy_to_user(admin.id, item, admin))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        run(main.copy_to_user("nobody", item, admin))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        run(main.copy_to_user(other.id, {"items": []}, admin))
    assert e.value.status_code == 400
    # one bad id fails the whole call before anything is written
    before = len(list(main._ctx_for(other).cards.glob("*.json")))
    with pytest.raises(HTTPException) as e:
        run(main.copy_to_user(other.id, {"items": [{"kind": "character", "id": card.id},
                                                   {"kind": "character", "id": "missing"}]}, admin))
    assert e.value.status_code == 404
    assert len(list(main._ctx_for(other).cards.glob("*.json"))) == before


def test_vrm_delete_refused_while_another_user_uses_it(admin, other):
    (main._vrm_dir() / "Shared.vrm").write_bytes(b"x")
    card = CharacterCard(name="Iris", vrm="Shared.vrm")
    main._save(card, main._ctx_for(other).cards)
    with pytest.raises(HTTPException) as e:
        run(main.delete_vrm("Shared.vrm", cur=main._ctx_for(admin)))
    assert e.value.status_code == 409 and "another user" in e.value.detail
