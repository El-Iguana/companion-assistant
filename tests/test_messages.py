"""Edit / delete / truncate one message (main.py) — and that they keep the two position
indices that memory folding depends on consistent: the chat's
``last_summarized_index`` and the pairing's assistant memory's ``swept_sessions[id]``.

    .venv/bin/python -m pytest -q tests/test_messages.py

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
from models.schema import CharacterCard, ChatMessage, ChatSession, User  # noqa: E402
from services import auth  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def cur():
    salt = auth.new_salt()
    u = User(username=f"t{os.urandom(4).hex()}", role="user", salt=salt,
             password_hash=auth.hash_password("x", salt))
    main._save_user(u)
    return main._ctx_for(u)


@pytest.fixture()
def chat(cur):
    card = CharacterCard(name="Iris")
    main._save(card, cur.cards)
    s = ChatSession(character_id=card.id, persona_id=main._default_persona(cur).id)
    s.messages = [ChatMessage(role="char", content="hello")]
    for i in range(1, 7):
        s.messages.append(ChatMessage(role="user" if i % 2 else "char", content=f"m{i}",
                                      swipes=[] if i % 2 else [f"m{i}", f"m{i}-alt"]))
    s.memory.last_summarized_index = 4
    s.memory.summary = "they talked"
    main._save(s, cur.sessions)
    mem = main._load_assistant_memory(cur, card.id, s.persona_id)
    mem.swept_sessions[s.id] = 5
    main._save_assistant_memory(cur, mem)
    return s


def _reload(cur, s):
    return main._load(cur.sessions, s.id, ChatSession)


def _mark(cur, s):
    return main._load_assistant_memory(cur, s.character_id, s.persona_id).swept_sessions.get(s.id)


def test_edit_user_message(cur, chat):
    mid = chat.messages[1].id
    run(main.edit_message(chat.id, mid, {"content": "  edited  "}, cur=cur))
    assert _reload(cur, chat).messages[1].content == "edited"


def test_edit_char_message_edits_the_shown_swipe(cur, chat):
    m = chat.messages[2]
    m.swipe_index = 1
    main._save(chat, cur.sessions)
    run(main.edit_message(chat.id, m.id, {"content": "rewritten"}, cur=cur))
    got = _reload(cur, chat).messages[2]
    assert got.content == "rewritten" and got.swipes == ["m2", "rewritten"]


def test_edit_refuses_empty(cur, chat):
    with pytest.raises(HTTPException):
        run(main.edit_message(chat.id, chat.messages[1].id, {"content": "  "}, cur=cur))


def test_delete_before_summary_shifts_both_indices(cur, chat):
    run(main.delete_message(chat.id, chat.messages[2].id, cur=cur))
    s = _reload(cur, chat)
    assert [m.content for m in s.messages] == ["hello", "m1", "m3", "m4", "m5", "m6"]
    assert s.memory.last_summarized_index == 3          # was 4; one earlier message gone
    assert _mark(cur, chat) == 4                         # was 5


def test_delete_after_marks_leaves_them(cur, chat):
    run(main.delete_message(chat.id, chat.messages[6].id, cur=cur))
    s = _reload(cur, chat)
    assert s.memory.last_summarized_index == 4 and _mark(cur, chat) == 5


def test_delete_refuses_the_greeting(cur, chat):
    with pytest.raises(HTTPException):
        run(main.delete_message(chat.id, chat.messages[0].id, cur=cur))


def test_truncate_above_summary_keeps_it(cur, chat):
    r = run(main.truncate_after(chat.id, chat.messages[5].id, cur=cur))
    s = _reload(cur, chat)
    assert r["removed"] == 1 and len(s.messages) == 6
    assert s.memory.summary == "they talked" and s.memory.last_summarized_index == 4
    assert _mark(cur, chat) == 5


def test_truncate_below_summary_resets_it(cur, chat):
    run(main.truncate_after(chat.id, chat.messages[1].id, cur=cur))
    s = _reload(cur, chat)
    assert [m.content for m in s.messages] == ["hello", "m1"]
    assert s.memory.summary == "" and s.memory.last_summarized_index == 0
    assert _mark(cur, chat) == 2                         # pulled back to the new end


def test_unknown_message_is_404(cur, chat):
    with pytest.raises(HTTPException) as exc:
        run(main.delete_message(chat.id, "nope", cur=cur))
    assert exc.value.status_code == 404
