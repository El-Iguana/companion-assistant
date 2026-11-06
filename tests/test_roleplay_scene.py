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
    monkeypatch.setattr(main.expressions, "classify", _neutral)   # no model in tests
    monkeypatch.setattr(main, "STREAM_KEEPALIVE_S", 0.05)
    return state


async def _neutral(*a, **k):
    return "neutral"


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


# --- the narrator as game master: side characters speak ---------------------------

def test_narrator_may_voice_other_people_but_never_her_or_the_user():
    card, persona = CharacterCard(name="Mira"), Persona(name="Sam")
    sys_prompt = prompt_builder.build_narrator_messages(
        card, persona, ChatSession(messages=[ChatMessage(role="user", content="Hi.")]))[0]["content"]
    assert "game master" in sys_prompt and "quotation marks" in sys_prompt
    assert "Never write a word Mira or Sam says" in sys_prompt


def test_narrator_sees_triggered_lorebook_entries_with_stage_gating():
    from models.schema import CharacterBook, LorebookEntry
    book = CharacterBook(entries=[
        LorebookEntry(keys=["Bram"], content="Old Bram, the gruff innkeeper."),
        LorebookEntry(keys=["Bram"], content="Bram's secret debt.", min_stage="Close")])
    card, persona = CharacterCard(name="Mira", character_book=book), Persona(name="Sam")
    s = ChatSession(messages=[ChatMessage(role="user", content="Where's Bram tonight?")])
    early = prompt_builder.build_narrator_messages(card, persona, s)[1]["content"]
    assert "World notes:\nOld Bram, the gruff innkeeper." in early and "secret debt" not in early
    rank = prompt_builder.STAGE_ORDER.index("Close")
    late = prompt_builder.build_narrator_messages(card, persona, s, stage_rank=rank)[1]["content"]
    assert "secret debt" in late


def test_trim_keeps_a_side_characters_own_paragraph():
    t = 'Thunder rolls.\n\n"Rum\'s out," Bram grumbles, setting a mug down in front of Sam.'
    assert prompt_builder.trim_narration(t, "Mira", "Sam") == t


def test_trim_cuts_where_the_narrator_starts_writing_her_or_the_user():
    t = 'Thunder rolls.\n\n"Rum\'s out," Bram grumbles.\n\nMira looks up. "Then whiskey."'
    assert prompt_builder.trim_narration(t, "Mira", "Sam") == 'Thunder rolls.\n\n"Rum\'s out," Bram grumbles.'
    t2 = 'Thunder rolls.\n\nThe door bangs; *Sam nods at the stranger.*'
    assert prompt_builder.trim_narration(t2, "Mira", "Sam") == "Thunder rolls."
    t3 = 'Thunder rolls.\n\n*Mira shivers.*'
    assert prompt_builder.trim_narration(t3, "Mira", "Sam") == "Thunder rolls."


def test_trim_caps_the_paragraphs_and_always_keeps_the_first():
    assert prompt_builder.trim_narration("A.\n\nB.\n\nC.\n\nD.", "Mira", "Sam") == "A.\n\nB.\n\nC."
    assert prompt_builder.trim_narration("Mira looks up.", "Mira", "Sam") == "Mira looks up."
    assert prompt_builder.trim_narration("", "Mira", "Sam") == ""


# ---- side characters ------------------------------------------------------------

def test_side_characters_on_by_default_and_the_narrator_may_voice_them(session):
    card, persona = CharacterCard(name="Hermione"), Persona(name="Brian")
    assert session.allow_side_characters is True
    n = prompt_builder.build_narrator_messages(card, persona, session)
    assert "play every other person" in n[0]["content"] and "line they say out loud" in n[1]["content"]


def test_side_characters_off_gives_the_narrator_nobody_to_play(session):
    card, persona = CharacterCard(name="Hermione"), Persona(name="Brian")
    session.allow_side_characters = False
    n = prompt_builder.build_narrator_messages(card, persona, session, "a storm rolls in")
    assert "Only the two of them" in n[0]["content"] and "play every other person" not in n[0]["content"]
    assert "bring in no one else" in n[1]["content"] and "line they say out loud" not in n[1]["content"]
    assert "a storm rolls in" in n[1]["content"]


def test_strip_dialogue_drops_whole_sentences_with_speech():
    text = ('Rain drums on the roof. A dockhand shoulders in. "Tide\'s turned early," he mutters.'
            '\n\n“Harbourmaster’s closing up.” The lamp gutters.')
    assert prompt_builder.strip_dialogue(text) == \
        "Rain drums on the roof. A dockhand shoulders in.\n\nThe lamp gutters."
    assert prompt_builder.strip_dialogue('"All hands!" someone shouts.') == ""
    assert prompt_builder.strip_dialogue("Thunder rolls over the hills.") == "Thunder rolls over the hills."


def test_narration_with_side_characters_off_keeps_no_dialogue(cur, session, model):
    session.allow_side_characters = False
    main._save(session, cur.sessions)
    model["tokens"] = ["Wind rattles the shutters. ", '"Close them!" a voice calls.']
    asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    assert _reload(cur, session).messages[-1].content == "Wind rattles the shutters."
    assert "Only the two of them" in model["prompts"][-1][0]["content"]


def test_narration_that_is_only_dialogue_is_an_error_with_side_characters_off(cur, session, model):
    session.allow_side_characters = False
    main._save(session, cur.sessions)
    model["tokens"] = ['"Who goes there?" a guard calls.']
    evs = asyncio.run(_run(main.narrate, SceneRequest(session_id=session.id), cur))
    assert any("error" in e for e in evs) and _roles(cur, session) == ["char", "user", "char"]


def test_options_are_saved_and_a_turn_does_not_undo_them(cur, session, model):
    from models.schema import SessionOptions

    r = asyncio.run(main.set_session_options(
        session.id, SessionOptions(session_id=session.id, allow_side_characters=False), cur=cur))
    assert r["allow_side_characters"] is False and _reload(cur, session).allow_side_characters is False
    stale = session                                   # loaded before the box was unticked
    assert stale.allow_side_characters is True
    main._save_turn(cur, stale)
    assert _reload(cur, session).allow_side_characters is False


# ---- your note guides her turn ---------------------------------------------------

def test_direction_steers_continue_and_is_not_saved(cur, session, model):
    asyncio.run(_run(main.chat, ChatRequest(session_id=session.id, input_text="", continue_turn=True,
                                            direction="she admits she is afraid"), cur))
    sent = model["prompts"][-1]
    tail = sent[-1]["content"]
    assert "Direction from" in tail and "she admits she is afraid" in tail
    assert "next reply" in tail
    s = _reload(cur, session)
    assert all("afraid" not in m.content for m in s.messages if m.role != "char")
    assert [m.role for m in s.messages] == ["char", "user", "char", "char"]


def test_direction_on_another_take_asks_for_a_rewrite_before_the_boundary(cur, session, model):
    asyncio.run(_run(main.chat, ChatRequest(session_id=session.id, input_text="", regenerate=True,
                                            direction="shorter, and more teasing"), cur))
    tail = model["prompts"][-1][-1]["content"]
    assert "rewritten reply" in tail and "shorter, and more teasing" in tail
    assert tail.index("Direction from") > tail.index("another take")
    bound = tail.find("Boundaries")
    if bound >= 0:
        assert tail.index("Direction from") < bound
    s = _reload(cur, session)
    assert len(s.messages) == 3 and len(s.messages[-1].swipes) == 2


def test_no_direction_no_note():
    s = ChatSession(messages=[ChatMessage(role="user", content="Hi.")])
    tail = prompt_builder.build_messages(CharacterCard(name="Hermione"), Persona(name="Brian"), s)[-1]
    assert "Direction from" not in tail["content"]
