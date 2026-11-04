"""Relationship grounding: moments that moved the relationship (muninn.episodic).

The relationship was numbers only: she "trusted" {{user}} without remembering
anything that earned it. A reply that moves it sharply, or crosses a milestone, is
now kept as a moment (what {{user}} said, her line), the latest one rides along in
the relationship note, and the Memory modal can review and remove them.

    .venv/bin/python -m pytest -q tests/test_relationship_grounding.py
"""
from __future__ import annotations

import asyncio

import main
from models.schema import RelationshipPreset
from muninn import episodic
from test_chat_stream import _events, _jobs, cur, model, session  # noqa: F401 — fixtures

REPLY = "Iris presses her hand to her heart. 'Nobody has ever done that for me.'"


def _setup(monkeypatch, feel="gratitude"):
    prompts = []

    async def tokens(messages, params):
        prompts.append(messages)
        yield REPLY

    async def classify(*a, **k):
        return feel

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.expressions, "classify", classify)
    return prompts


async def _say(cur, session, text):
    from models.schema import ChatRequest
    resp = await main.chat(ChatRequest(session_id=session.id, input_text=text), cur=cur)
    out = _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])
    await _jobs()
    return out


def _mem(cur, session):
    return main._load_character_memory(cur, session.character_id, session.persona_id)


def test_a_strong_reply_is_a_moment_and_grounds_the_next_prompt(cur, session, model, monkeypatch):
    prompts = _setup(monkeypatch)
    asyncio.run(_say(cur, session, "I paid off your debt to the guild."))
    moments = _mem(cur, session).moments
    assert len(moments) == 1
    assert moments[0].said == "I paid off your debt to the guild."
    assert moments[0].shown.startswith("Iris presses her hand to her heart.")
    asyncio.run(_say(cur, session, "How are you feeling?"))
    system = prompts[-1][0]["content"]
    assert 'A moment Iris remembers' in system and "I paid off your debt to the guild." in system


def test_a_neutral_reply_keeps_nothing(cur, session, model, monkeypatch):
    _setup(monkeypatch, feel="neutral")
    asyncio.run(_say(cur, session, "Nice weather."))
    assert _mem(cur, session).moments == []


def test_scenario_chats_keep_no_moments(cur, session, model, monkeypatch):
    _setup(monkeypatch)
    s = main._load(cur.sessions, session.id, main.ChatSession)
    s.relationship = main.relationship.from_preset(RelationshipPreset(), s.character_id, s.persona_id)
    main._save(s, cur.sessions)
    asyncio.run(_say(cur, session, "I paid off your debt."))
    assert _mem(cur, session).moments == []


def test_the_modal_removes_moments_and_a_save_without_them_leaves_them(cur, session, model):
    cid, pid = session.character_id, session.persona_id
    m = main._load_character_memory(cur, cid, pid)
    m.moments = [episodic.Moment(said="a"), episodic.Moment(said="b")]
    main._save_character_memory(cur, m)
    asyncio.run(main.put_character_memory({"character_id": cid, "persona_id": pid, "summary": "x"}, cur=cur))
    assert [x.said for x in _mem(cur, session).moments] == ["a", "b"]
    asyncio.run(main.put_character_memory({"character_id": cid, "persona_id": pid,
                                           "moments": [m.moments[1].id]}, cur=cur))
    assert [x.said for x in _mem(cur, session).moments] == ["b"]


def test_a_moment_removed_while_a_reply_streams_stays_removed(cur, session, model, monkeypatch):
    cid, pid = session.character_id, session.persona_id
    m = main._load_character_memory(cur, cid, pid)
    m.moments = [episodic.Moment(said="old")]
    main._save_character_memory(cur, m)
    hold, streaming = asyncio.Event(), asyncio.Event()

    async def slow(messages, params):
        streaming.set()
        await hold.wait()
        yield REPLY

    async def classify(*a, **k):
        return "gratitude"

    monkeypatch.setattr(main.kobold, "chat_generate_stream", slow)
    monkeypatch.setattr(main.expressions, "classify", classify)

    async def go():
        turn = asyncio.create_task(_say(cur, session, "I paid off your debt."))
        await streaming.wait()
        await main.put_character_memory({"character_id": cid, "persona_id": pid, "moments": []}, cur=cur)
        hold.set()
        await turn

    asyncio.run(go())
    assert [x.said for x in _mem(cur, session).moments] == ["I paid off your debt."]
