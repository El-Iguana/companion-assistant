"""Roleplay scene tools: narrator events, Continue, and drafts of the user's reply.

main.narrate writes a neutral scene event into the chat (role "narrator"); main.chat with
continue_turn has her take a turn with no new user message; main.draft_reply writes the
user's next message for the composer and saves nothing. Assistant chats get none of it.

    .venv/bin/python -m pytest -q tests/test_roleplay_scene.py

Runs against a throwaway library (PANTHEON_DATA_DIR is set before ``main`` is imported).
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
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402
from models.schema import (  # noqa: E402
    CharacterCard, ChatMessage, ChatRequest, ChatSession, Persona, SceneRequest, User,
)
from services import auth, memory, prompt_builder  # noqa: E402


@pytest.fixture()
def cur():
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return main._ctx_for(u)


def _session(cur, mode="roleplay"):
    card = CharacterCard(name="Hermione", chat_mode=mode)
    main._save(card, cur.cards)
    s = ChatSession(character_id=card.id, persona_id=main._default_persona(cur).id)
    s.messages = [ChatMessage(role="char", content="Hello."), ChatMessage(role="user", content="Hi."),
                  ChatMessage(role="char", content="Shall we study?")]
    main._save(s, cur.sessions)
    return s


@pytest.fixture()
def session(cur):
    return _session(cur)


@pytest.fixture()
def model(monkeypatch):
    """Canned tokens per call; records the prompts it was given."""
    state = {"tokens": ["A", " bell", " rings."], "delay": 0.0, "prompts": []}

    async def tokens(messages, params):
        state["prompts"].append(messages)
        await asyncio.sleep(state["delay"])
        for t in state["tokens"]:
            yield t

    async def no_summary(*a, **k):
        return None

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.memory, "summarize", no_summary)
    monkeypatch.setattr(main, "STREAM_KEEPALIVE_S", 0.05)
    return state


def _events(resp_chunks) -> list[dict]:
    out = []
    for block in "".join(resp_chunks).split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


async def _run(handler, req, cur) -> list[dict]:
    resp = await handler(req, cur=cur)
    return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])


async def _jobs():
    while main._memory_tasks:
        await asyncio.gather(*list(main._memory_tasks))


def _reload(cur, s) -> ChatSession:
    return main._load(cur.sessions, s.id, ChatSession)


def _roles(cur, s) -> list[str]:
    return [m.role for m in _reload(cur, s).messages]


# ---- prompts ------------------------------------------------------------------

def _prompt(card, session):
    return prompt_builder.build_messages(card, Persona(name="Brian"), session)


def test_narration_reaches_her_as_a_labelled_system_turn_and_she_reacts_to_it():
    card = CharacterCard(name="Hermione")
    s = ChatSession(messages=[ChatMessage(role="user", content="Hi."),
                              ChatMessage(role="narrator", content="A bell rings.")])
    msgs = _prompt(card, s)
    assert {"role": "system", "content": "[Narrator] A bell rings."} in msgs
    assert not any(m["role"] == "user" and "bell" in m["content"] for m in msgs)
    tail = msgs[-1]["content"]
    assert "Something just happened in the scene" in tail and "A bell rings." in tail
    assert "Reply to Brian's latest message" not in tail


def test_continue_note_when_she_spoke_last():
    card = CharacterCard(name="Hermione")
    s = ChatSession(messages=[ChatMessage(role="user", content="Hi."),
                              ChatMessage(role="char", content="Shall we?")])
    assert "Brian has not spoken yet" in _prompt(card, s)[-1]["content"]


def test_an_assistant_keeps_the_plain_focus_note():
    card = CharacterCard(name="Jarvis", chat_mode="assistant")
    s = ChatSession(messages=[ChatMessage(role="user", content="Time?"),
                              ChatMessage(role="char", content="Noon.")])
    tail = _prompt(card, s)[-1]["content"]
    assert "has not spoken yet" not in tail and "Reply to Brian's latest message" in tail


def test_memory_transcript_labels_the_narrator():
    t = memory._transcript([ChatMessage(role="narrator", content="Rain."),
                            ChatMessage(role="user", content="Hi.")], "Hermione", "Brian")
    assert t == "Narrator: Rain.\nBrian: Hi."


def test_narrator_and_draft_prompts_carry_the_scene_and_the_direction():
    card, persona = CharacterCard(name="Hermione"), Persona(name="Brian")
    s = ChatSession(messages=[ChatMessage(role="char", content="Shall we?")])
    n = prompt_builder.build_narrator_messages(card, persona, s, "a storm rolls in")
    assert "narrator" in n[0]["content"] and "Hermione: Shall we?" in n[1]["content"]
    assert "a storm rolls in" in n[1]["content"]
    d = prompt_builder.build_draft_messages(card, persona, s, "agree, nervously")
    assert "Brian's next message" in d[0]["content"] and "agree, nervously" in d[1]["content"]


def test_clean_piece_drops_labels_and_wrapping_quotes():
    assert prompt_builder.clean_piece('Narrator: A bell rings.', ["Narrator"]) == "A bell rings."
    assert prompt_builder.clean_piece('*Narrator: Rain falls.*', ["Narrator"]) == "*Rain falls.*"
    assert prompt_builder.clean_piece('"Sure, let\'s go."', ["Brian"]) == "Sure, let's go."
    assert prompt_builder.clean_piece('"Yes," I say. "Now."', ["Brian"]) == '"Yes," I say. "Now."'


# ---- narrate --------------------------------------------------------------------

def test_narrate_saves_a_narrator_message_and_leaves_her_state_alone(cur, session, model):
    before = _reload(cur, session)
    evs = asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    assert evs[-1].get("done")
    s = _reload(cur, session)
    assert s.messages[-1].role == "narrator" and s.messages[-1].content == "A bell rings."
    assert s.mood == before.mood and s.relationship == before.relationship


def test_another_take_replaces_only_the_last_narration(cur, session, model):
    asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    model["tokens"] = ["Thunder."]
    asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id, replace=True), cur))
    s = _reload(cur, session)
    assert [m.role for m in s.messages] == ["char", "user", "char", "narrator"]
    assert s.messages[-1].content == "Thunder."


def test_replace_without_a_trailing_narration_just_adds_one(cur, session, model):
    asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id, replace=True), cur))
    assert _roles(cur, session) == ["char", "user", "char", "narrator"]


def test_stopping_the_narrator_saves_nothing(cur, session, model):
    model["delay"] = 0.3

    async def go():
        resp = await main.narrate(SceneRequest(session_id=session.id), cur=cur)
        body = resp.body_iterator
        async for c in body:
            if '"ping"' in (c if isinstance(c, str) else c.decode()):
                break
        await body.aclose()
        await asyncio.sleep(0.5)

    asyncio.run(go())
    assert _roles(cur, session) == ["char", "user", "char"]


def test_an_edit_during_narration_is_not_rolled_back(cur, session, model):
    model["delay"] = 0.2

    async def go():
        resp = await main.narrate(SceneRequest(session_id=session.id), cur=cur)
        body = resp.body_iterator
        first = await body.__anext__()           # a ping: the model is still "thinking"
        assert "ping" in (first if isinstance(first, str) else first.decode())
        s = _reload(cur, session)
        await main.edit_message(session.id, s.messages[1].id, {"content": "changed"}, cur=cur)
        async for _ in body:
            pass

    asyncio.run(go())
    s = _reload(cur, session)
    assert s.messages[1].content == "changed" and s.messages[-1].role == "narrator"


def test_an_empty_narration_is_an_error_not_a_blank_message(cur, session, model):
    model["tokens"] = ["Narrator:", "  "]
    evs = asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    assert "error" in evs[-1]
    assert _roles(cur, session) == ["char", "user", "char"]


# ---- continue ---------------------------------------------------------------------

def test_continue_adds_her_reply_with_no_user_message(cur, session, model):
    asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    model["tokens"] = ["She", " looks", " up."]
    evs = asyncio.run(_run(main.chat, ChatRequest(session_id=session.id, input_text="",
                                                  continue_turn=True), cur))
    asyncio.run(_jobs())
    assert evs[-1].get("done")
    s = _reload(cur, session)
    assert [m.role for m in s.messages] == ["char", "user", "char", "narrator", "char"]
    assert s.messages[-1].content == "She looks up."
    assert "Something just happened in the scene" in model["prompts"][-1][-1]["content"]


# ---- draft -----------------------------------------------------------------------

def test_draft_returns_the_users_reply_and_saves_nothing(cur, session, model):
    model["tokens"] = ["User", ": Sure,", " let's", " start."]
    before = _reload(cur, session).model_dump()
    evs = asyncio.run(_run(main.draft_reply, SceneRequest(session_id=session.id), cur))
    assert evs[-1] == {"done": True, "text": "Sure, let's start."}
    assert _reload(cur, session).model_dump() == before


# ---- assistant chats ---------------------------------------------------------------

def test_assistant_chats_get_none_of_it(cur, model):
    s = _session(cur, mode="assistant")

    async def go():
        for call in (lambda: main.narrate(SceneRequest(session_id=s.id), cur=cur),
                     lambda: main.draft_reply(SceneRequest(session_id=s.id), cur=cur),
                     lambda: main.chat(ChatRequest(session_id=s.id, input_text="",
                                                   continue_turn=True), cur=cur)):
            with pytest.raises(HTTPException) as e:
                await call()
            assert e.value.status_code == 400

    asyncio.run(go())
    assert _roles(cur, s) == ["char", "user", "char"]
