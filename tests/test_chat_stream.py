"""The chat stream keeps itself alive while a turn finishes (main.chat).

After the last token a turn still runs model calls — expression, relationship activity,
the memory summary and long-term fold — and used to send nothing until ``done``. pytincture
closes a BFF stream after 30 s of silence, which cancelled the turn before it was saved:
the reply streamed in full, then vanished on reload. These tests stand in a fake model
whose post-reply step is slower than the keep-alive interval.

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
from models.schema import CharacterCard, ChatRequest, ChatSession, User  # noqa: E402
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
    main._save(s, cur.sessions)
    return s


@pytest.fixture()
def slow_turn(monkeypatch):
    """Tokens arrive at once; the memory step after them is silent for 0.3 s."""
    async def tokens(messages, params):
        for t in ("Hello", " there", "."):
            yield t

    async def slow_summary(*a, **k):
        await asyncio.sleep(0.3)

    monkeypatch.setattr(main.kobold, "chat_generate_stream", tokens)
    monkeypatch.setattr(main.memory, "summarize", slow_summary)
    monkeypatch.setattr(main, "STREAM_KEEPALIVE_S", 0.05)


def _events(chunks: list[str]) -> list[dict]:
    out = []
    for block in "".join(chunks).split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_slow_finish_sends_pings_then_done_and_saves(cur, session, slow_turn):
    async def go():
        resp = await main.chat(ChatRequest(session_id=session.id, input_text="hi"), cur=cur)
        return [c if isinstance(c, str) else c.decode() async for c in resp.body_iterator]

    evs = _events(asyncio.run(go()))
    assert "".join(e["token"] for e in evs if "token" in e) == "Hello there."
    last_token = max(i for i, e in enumerate(evs) if "token" in e)
    assert any(e.get("ping") for e in evs[last_token:]), "no keep-alive during the silent finish"
    assert evs[-1].get("done")
    saved = main._load(cur.sessions, session.id, ChatSession)
    assert [m.content for m in saved.messages if m.role == "char"] == ["Hello there."]


def test_client_leaving_mid_finish_still_does_not_save(cur, session, slow_turn):
    """Stop / a closed tab keeps its old meaning: the turn is not saved."""
    async def go():
        resp = await main.chat(ChatRequest(session_id=session.id, input_text="hi"), cur=cur)
        body = resp.body_iterator
        async for c in body:
            if '"ping"' in (c if isinstance(c, str) else c.decode()):
                break                     # leave during the silent finish
        await body.aclose()
        await asyncio.sleep(0.5)          # past when the turn would have saved

    asyncio.run(go())
    saved = main._load(cur.sessions, session.id, ChatSession)
    assert not [m for m in saved.messages if m.role == "char"]
