"""The example assistant every new library starts with (main._seed_library), and JSON
written by the roleplay app this repo was split from still loading here.

    .venv/bin/python -m pytest -q tests/test_seed_and_legacy.py
"""
from __future__ import annotations

import asyncio
import json
import os

import main
from models.schema import CharacterCard, ChatSession, Persona, User
from services import auth, prompt_builder
from test_chat_stream import cur  # noqa: F401 — fixture


def _admin() -> User:
    salt = auth.new_salt()
    u = User(username=f"a{os.urandom(4).hex()}", role="admin", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return u


def _cards(user_id: str) -> list[CharacterCard]:
    return main._list(main._ctx_for(main._user_by_id(user_id)).cards, CharacterCard)


def test_a_new_user_starts_with_mira():
    u = asyncio.run(main.create_user({"username": f"n{os.urandom(4).hex()}", "password": "pass"},
                                     _admin()))
    mira = next(c for c in _cards(u["id"]) if c.id == "mira")
    assert mira.name == "Mira" and mira.wake_word == "Mira"
    assert mira.vrm == "baseline.vrm" and mira.avatar_mode == "vrm"   # redistributable model
    assert mira.vrm_still is None and mira.portrait_source == ""
    assert mira.created_at == mira.updated_at                          # stamped at seed time


def test_mira_is_not_brought_back_after_she_is_deleted():
    u = asyncio.run(main.create_user({"username": f"d{os.urandom(4).hex()}", "password": "pass"},
                                     _admin()))
    cur_ = main._ctx_for(main._user_by_id(u["id"]))
    asyncio.run(main.delete_card("mira", cur=cur_))
    main._ctx_for(main._user_by_id(u["id"]))          # every later request builds a Ctx
    assert not any(c.id == "mira" for c in _cards(u["id"]))


def test_seed_card_bundles_no_other_model_and_no_still():
    raw = json.loads((main.SEED / "characters" / "mira.json").read_text())
    assert raw["vrm"] == "baseline.vrm"
    assert not {"vrm_still", "vrm_still_of", "chat_mode", "power_dynamic", "social_acceptance",
                "created_at", "updated_at"} & set(raw)
    assert (main.BASE / "data" / "vrm" / "baseline.vrm").is_file()


def test_seeding_keeps_a_card_already_there(tmp_path):
    (tmp_path / "characters").mkdir()
    mine = CharacterCard(id="mira", name="My Mira")
    main._save(mine, tmp_path / "characters")
    assert main._seed_library(tmp_path) == 0
    assert main._load(tmp_path / "characters", "mira", CharacterCard).name == "My Mira"


def test_roleplay_app_json_loads_and_prompts_as_an_assistant(cur):  # noqa: F811
    card = CharacterCard.model_validate({
        "name": "Hermione", "chat_mode": "roleplay", "power_dynamic": 20, "pronouns": "she/her",
        "character_book": {"entries": [{"keys": ["library"], "content": "The library is vast.",
                                        "min_stage": "Close"}]}})
    main._save(card, cur.cards)
    persona = main._default_persona(cur)
    session = ChatSession.model_validate({
        "character_id": card.id, "persona_id": persona.id,
        "relationship": {"character_id": card.id, "persona_id": persona.id, "trust": 40},
        "pending_milestone": "first kiss", "allow_side_characters": False,
        "memory": {"summary": "Story prose from the roleplay app."},
        "messages": [{"role": "char", "content": "Hello."},
                     {"role": "narrator", "content": "A bell rings in the distance."},
                     {"role": "user", "content": "Where is the library?"}]})
    main._save(session, cur.sessions)
    loaded = main._load(cur.sessions, session.id, ChatSession)
    msgs = prompt_builder.build_messages(card, persona, loaded)
    text = "\n".join(m["content"] for m in msgs)
    assert "spoken aloud" in msgs[0]["content"].lower()             # the assistant prompt
    assert "The library is vast." in text                            # no stage gate any more
    assert "A bell rings" not in text and "Story prose" not in text  # roleplay residue left out
    assert [m["role"] for m in msgs[1:-1]] == ["assistant", "user"]
    assert isinstance(persona, Persona)


def test_any_library_gets_the_baseline_model_mira_uses():
    assert (main._vrm_dir() / "baseline.vrm").is_file()
