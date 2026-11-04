"""The chat stream and the memory step after it (main.chat, main._memory_job).

A turn saves and sends ``done`` as soon as the reply is complete. The memory summary and
long-term fold run afterwards in a background job, so they can overlap the next turn, an
edit or a truncate — these tests pin down that none of those loses data. The stream also
keeps itself alive through silence: pytincture closes a BFF stream after 30 s without an
item, which once cancelled turns before they were saved (replies streamed in full, then
vanished on reload).

    .venv/bin/python -m pytest -q tests/test_chat_stream.py

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

import main  # noqa: E402
from models.schema import CharacterCard, ChatMessage, ChatRequest, ChatSession, User  # noqa: E402
from services import auth  # noqa: E402


@pytest.fixture()
def cur():
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return main._ctx_for(u)


@pytest.fixture()
def session(cur):
    card = CharacterCard(name="Iris")
    main._save(card, cur.cards)
    s = ChatSession(character_id=card.id, persona_id=main._default_persona(cur).id)
    s.messages = [ChatMessage(role="char", content="hello"), ChatMessage(role="user", content="m1"),
                  ChatMessage(role="char", content="m2")]
    main._save(s, cur.sessions)
    return s


class Gate:
    """Holds the fake summary step open until the test releases it."""
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()


@pytest.fixture()
def model(monkeypatch):
    """Instant tokens; a summary step that folds everything but the last message, and
    waits on a Gate when one is set."""
    state = {"gate": None, "token_delay": 0.0}

    async def tokens(messages, params):
        await asyncio.sleep(state["token_delay"])
        for t in ("Hello", " there", "."):
            yield t

    async def summarize(kobold, session, card, persona, **k):
        gate = state["gate"]
        if gate:
            gate.started.set()
            await gate.release.wait()
        session.memory.summary = "S"
        session.memory.last_summarized_index = len(session.messages) - 1

    async def no_fold(*a, **k):
        return False

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.memory, "summarize", summarize)
    monkeypatch.setattr(main.memory, "fold_into_longterm", no_fold)
    monkeypatch.setattr(main.expressions, "classify", _neutral)   # no model in tests
    monkeypatch.setattr(main, "STREAM_KEEPALIVE_S", 0.05)
    return state


async def _neutral(*a, **k):
    return "neutral"


def _events(chunks: list[str]) -> list[dict]:
    out = []
    for block in "".join(chunks).split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


async def _turn(cur, session, text="hi") -> list[dict]:
    resp = await main.chat(ChatRequest(session_id=session.id, input_text=text), cur=cur)
    return _events([c if isinstance(c, str) else c.decode() async for c in resp.body_iterator])


async def _jobs():
    while main._memory_tasks:
        await asyncio.gather(*list(main._memory_tasks))
        # Yield once: awaiting already-finished tasks doesn't, so their done-callbacks
        # (which remove them from the set) never ran and this spun forever.
        await asyncio.sleep(0)


def _reload(cur, s) -> ChatSession:
    return main._load(cur.sessions, s.id, ChatSession)


def test_done_arrives_before_the_memory_step_and_the_reply_is_saved(cur, session, model):
    async def go():
        model["gate"] = gate = Gate()
        evs = await _turn(cur, session)            # returns while the summary is held open
        assert evs[-1].get("done")
        saved = _reload(cur, session)
        assert saved.messages[-1].content == "Hello there."
        assert saved.memory.summary == ""          # memory hasn't run yet
        await gate.started.wait()
        gate.release.set()
        await _jobs()

    asyncio.run(go())
    assert _reload(cur, session).memory.summary == "S"


def test_next_turn_during_the_memory_step_loses_nothing(cur, session, model):
    async def go():
        model["gate"] = gate = Gate()
        await _turn(cur, session, "first")
        await gate.started.wait()                  # job 1 is mid-summary
        model["gate"] = None
        await _turn(cur, session, "second")        # the next turn saves meanwhile
        gate.release.set()
        await _jobs()

    asyncio.run(go())
    s = _reload(cur, session)
    assert [m.content for m in s.messages if m.role == "user"][-2:] == ["first", "second"]
    assert s.memory.summary == "S"
    assert s.memory.last_summarized_index == len(s.messages) - 1   # job 2 re-ran on the fresh copy


def test_stale_turn_save_does_not_roll_back_a_newer_summary(cur, session):
    stale = _reload(cur, session)
    fresh = _reload(cur, session)
    fresh.memory.summary, fresh.memory.last_summarized_index = "S", 2
    main._save(fresh, cur.sessions)
    stale.messages.append(ChatMessage(role="user", content="late"))
    main._save_turn(cur, stale)
    s = _reload(cur, session)
    assert s.messages[-1].content == "late" and s.memory.summary == "S"


def test_editing_a_folded_turn_during_the_memory_step_drops_the_stale_summary(cur, session, model):
    async def go():
        model["gate"] = gate = Gate()
        await _turn(cur, session)
        await gate.started.wait()
        s = _reload(cur, session)
        await main.edit_message(session.id, s.messages[1].id, {"content": "changed"}, cur=cur)
        gate.release.set()
        await _jobs()

    asyncio.run(go())
    s = _reload(cur, session)
    assert s.messages[1].content == "changed" and s.memory.summary == ""


def test_silence_before_the_first_token_sends_keepalives(cur, session, model):
    model["token_delay"] = 0.3                     # a long prompt being processed
    evs = asyncio.run(_turn(cur, session))
    first_token = min(i for i, e in enumerate(evs) if "token" in e)
    assert any(e.get("ping") for e in evs[:first_token])
    assert evs[-1].get("done")


def test_client_leaving_mid_stream_does_not_save(cur, session, model):
    """Stop / a closed tab keeps its meaning: the turn is not saved."""
    model["token_delay"] = 0.3

    async def go():
        resp = await main.chat(ChatRequest(session_id=session.id, input_text="hi"), cur=cur)
        body = resp.body_iterator
        async for c in body:
            if '"ping"' in (c if isinstance(c, str) else c.decode()):
                break                              # leave before the reply exists
        await body.aclose()
        await asyncio.sleep(0.5)

    asyncio.run(go())
    assert _reload(cur, session).messages[-1].content == "m2"


def test_a_memory_edit_saved_while_a_reply_streams_is_kept(cur, session, model, monkeypatch):
    """A turn never changes memory; the Memory modal and the memory job do. The turn
    used to write back the memory it loaded before the reply, losing an edit saved
    while the reply streamed (it only adopted the stored memory when the summary
    index had moved)."""
    hold = asyncio.Event()
    streaming = asyncio.Event()

    async def slow_tokens(messages, params):
        streaming.set()
        await hold.wait()
        yield "Hello."

    async def no_summary(*a, **k):      # the job folds into the stored summary; not under test
        return None

    monkeypatch.setattr(main.kobold, "chat_generate_stream", slow_tokens)
    monkeypatch.setattr(main.memory, "summarize", no_summary)

    async def go():
        turn = asyncio.create_task(_turn(cur, session))
        await streaming.wait()
        await main.set_memory(session.id, {"pinned": ["Afraid of deep water"],
                                           "summary": "Edited by hand."}, cur=cur)
        hold.set()
        await turn
        await _jobs()

    asyncio.run(go())
    saved = _reload(cur, session)
    assert saved.memory.pinned == ["Afraid of deep water"]
    assert saved.memory.summary == "Edited by hand."
    assert saved.messages[-1].content == "Hello."
