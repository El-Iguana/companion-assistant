"""Mood and the relationship move for roleplay cards without expression sprites.

The emotion classifier used to run only to pick a sprite, so a card with no sprites fed
"" into relationship.update() every turn and never left Stranger (Hermione: 49 replies,
every axis still 0). Now it reads the feeling from the full emotion set for those cards.

    .venv/bin/python -m pytest -q tests/test_relationship_feel.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PANTHEON_DATA_DIR", tempfile.mkdtemp(prefix="pantheon-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import main  # noqa: E402
from models.schema import CharacterCard, ChatMessage, ChatRequest, ChatSession, User  # noqa: E402
from services import auth, expressions  # noqa: E402


@pytest.fixture()
def cur():
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return main._ctx_for(u)


@pytest.fixture()
def model(monkeypatch):
    calls = []

    async def tokens(messages, params):
        for t in ("You", " came", " back."):
            yield t

    async def classify(kobold, reply, labels=None):
        calls.append(list(labels or []))
        return "admiration"

    async def nothing(*a, **k):
        return None

    async def no_fold(*a, **k):
        return False

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.expressions, "classify", classify)
    monkeypatch.setattr(main.memory, "summarize", nothing)
    monkeypatch.setattr(main.memory, "fold_into_longterm", no_fold)
    return calls


def _session(cur, mode="roleplay", sprites=None):
    card = CharacterCard(name="Hermione", chat_mode=mode, expressions=sprites or {})
    main._save(card, cur.cards)
    s = ChatSession(character_id=card.id, persona_id=main._default_persona(cur).id)
    s.messages = [ChatMessage(role="char", content="Hello.")]
    main._save(s, cur.sessions)
    return card, s


def _turn(cur, s, **kw):
    async def go():
        resp = await main.chat(ChatRequest(session_id=s.id, input_text="hi", **kw), cur=cur)
        out = "".join([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
        while main._memory_tasks:
            await asyncio.gather(*list(main._memory_tasks))
        return [json.loads(x[6:]) for x in out.split("\n") if x.startswith("data: ")]
    return asyncio.run(go())


def test_a_card_without_sprites_moves_off_zero(cur, model):
    card, s = _session(cur)
    evs = _turn(cur, s)
    assert model == [expressions.FULL_EMOTIONS]           # read from the full set
    rel = main._load_relationship(cur, card.id, s.persona_id)
    assert rel.affection > 0 and rel.trust > 0 and rel.attraction > 0
    saved = main._load(cur.sessions, s.id, ChatSession)
    assert saved.mood.mood == "admiration"
    assert saved.messages[-1].expression is None          # no sprite to show
    assert evs[-1].get("done") and evs[-1].get("relationship")


def test_cards_with_sprites_still_use_their_own_labels(cur, model):
    card, s = _session(cur, sprites={"neutral": "x", "happy": "y"})
    _turn(cur, s)
    assert model == [["neutral", "happy"]]


def test_assistants_are_not_classified(cur, model):
    _card, s = _session(cur, mode="assistant")
    _turn(cur, s)
    assert model == []


def test_a_reroll_does_not_classify_or_move_anything(cur, model):
    card, s = _session(cur)
    s.messages.append(ChatMessage(role="user", content="hi"))
    s.messages.append(ChatMessage(role="char", content="Old take."))
    main._save(s, cur.sessions)
    _turn(cur, s, regenerate=True)
    assert model == []
    assert main._load_relationship(cur, card.id, s.persona_id).affection == 0
