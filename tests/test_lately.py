""""Lately with this bartender" (Memory Phase 7, #18): a new chat knows the end of her last one.

    .venv/bin/python -m pytest -q tests/test_lately.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import main
from models.schema import CharacterCard, ChatMessage, ChatSession
from test_chat_stream import _events, cur  # noqa: F401 — fixtures


def _setup(cur, when: datetime | None = None):
    mira, rex = CharacterCard(name="Mira"), CharacterCard(name="Rex")
    main._save(mira, cur.cards)
    main._save(rex, cur.cards)
    persona = main._default_persona(cur)
    old = ChatSession(character_id=mira.id, persona_id=persona.id, messages=[
        ChatMessage(role="user", content="Remind me which hotel we picked for Lisbon?"),
        ChatMessage(role="char", content="The Memmo Alfama, the one with the rooftop pool."),
        ChatMessage(role="user", content="Right, thanks."), ChatMessage(role="char", content="Any time.")])
    if when:
        old.updated_at = when.isoformat()
    main._save(old, cur.sessions)
    return mira, rex, persona


def _new_chat(cur, card, persona, text="What was that hotel again?"):
    return ChatSession(character_id=card.id, persona_id=persona.id, messages=[ChatMessage(role="user", content=text)])


def test_a_new_chat_carries_the_end_of_her_last_one(cur):
    mira, rex, persona = _setup(cur)
    got = main.memory_for_turn(cur, mira, persona, _new_chat(cur, mira, persona))
    assert "Memmo Alfama" in got["trailing"] and "earlier today" in got["trailing"]
    assert "Mira: The Memmo Alfama" in got["trailing"] and f"{persona.name}: Right, thanks." in got["trailing"]
    assert "Memmo" not in got["system"]                     # per turn: after the history, never in the system block
    assert main.memory_for_turn(cur, rex, persona, _new_chat(cur, rex, persona))["trailing"] == ""   # hers only


def test_not_the_chat_itself_and_not_after_a_week(cur):
    mira, _, persona = _setup(cur, when=datetime.now() - timedelta(days=9))
    assert main.memory_for_turn(cur, mira, persona, _new_chat(cur, mira, persona))["trailing"] == ""
    same = main._list(cur.sessions, ChatSession)[0]
    assert main.memory_for_turn(cur, mira, persona, same)["trailing"] == ""


def test_it_reaches_the_prompt_after_the_history(cur, monkeypatch):
    mira, _, persona = _setup(cur)
    s = _new_chat(cur, mira, persona)
    main._save(s, cur.sessions)
    seen = {}

    async def model(messages, params):
        seen["messages"] = messages
        yield "The Memmo Alfama."
    monkeypatch.setattr(main.kobold, "chat_generate_stream", model)
    monkeypatch.setattr(main.assistant_memory, "fold", lambda *a, **k: asyncio.sleep(0, False))

    async def go():
        resp = await main.chat(main.ChatRequest(session_id=s.id, input_text="What was that hotel again?"), cur=cur)
        return [c async for c in resp.body_iterator]
    asyncio.run(go())
    last = seen["messages"][-1]
    assert last["role"] == "system" and "Lately with Mira" in last["content"] and "Memmo" in last["content"]
    assert "Memmo" not in seen["messages"][0]["content"]
