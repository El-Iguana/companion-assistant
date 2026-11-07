"""A memory fold doesn't overwrite an edit made while it ran (main._store_fold).

A fold (after a turn, or the idle sweep) loads the pairing's memory, spends up to a
minute on model calls, then used to save it whole: anything edited in the Memory
modal meanwhile was lost. And the assistant sweep saved the chat session it had
loaded before the fold, erasing a message sent while it ran.

    .venv/bin/python -m pytest -q tests/test_fold_merge.py
"""
from __future__ import annotations

import asyncio

import main
from models.schema import AssistantMemory, ChatMessage, ChatSession, MemoryFact
from test_chat_stream import cur, session  # noqa: F401 — fixtures


def _pair(cur, session):
    return session.character_id, session.persona_id


def test_fold_keeps_an_edit_made_while_it_ran(cur, session, monkeypatch):
    cid, pid = _pair(cur, session)
    mem = main._load_assistant_memory(cur, cid, pid)
    old_auto = MemoryFact(text="Eli likes tea", origin="auto")
    mem.facts = [old_auto, MemoryFact(text="Eli has a cat", origin="auto")]
    mem.notes = ["old thread"]
    main._save_assistant_memory(cur, mem)

    async def fold(kobold, m, s, card, persona, **k):
        # The user saves an edit in the Memory modal while the model works.
        await main.put_assistant_memory({"character_id": cid, "persona_id": pid, "facts": [
            {"id": m.facts[1].id, "text": "Eli has two cats"},      # edited
            {"text": "Eli is allergic to shellfish"},               # added by hand
            {"id": old_auto.id, "text": "Eli likes tea"},
        ], "directives": [{"text": "Call me Eli"}]}, cur=cur)
        m.facts = [f for f in m.facts if f.id != old_auto.id]       # the fold evicts one
        m.facts.append(MemoryFact(text="Eli plays bass", origin="auto"))
        m.notes = ["old thread", "new thread"]
        m.swept_sessions[s.id] = 3
        return True

    monkeypatch.setattr(main.assistant_memory, "fold", fold)
    asyncio.run(main.sweep_session(session.id, cur=cur))
    saved = main._load_assistant_memory(cur, cid, pid)
    texts = [f.text for f in saved.facts]
    assert "Eli has two cats" in texts and "Eli is allergic to shellfish" in texts   # the edit
    assert "Eli plays bass" in texts                                               # the fold's
    assert "Eli likes tea" not in texts                                            # evicted
    assert [d.text for d in saved.directives] == ["Call me Eli"]                   # kept
    assert saved.notes == ["old thread", "new thread"]       # not edited: the fold's win
    assert saved.swept_sessions[session.id] == 3


def test_edited_notes_beat_the_folds(cur, session, monkeypatch):
    cid, pid = _pair(cur, session)

    async def fold(kobold, m, s, card, persona, **k):
        await main.put_assistant_memory({"character_id": cid, "persona_id": pid,
                                         "notes": ["corrected by hand"]}, cur=cur)
        m.notes = ["the fold's version"]
        m.swept_sessions[s.id] = 3
        return True

    monkeypatch.setattr(main.assistant_memory, "fold", fold)
    asyncio.run(main.sweep_session(session.id, cur=cur))
    assert main._load_assistant_memory(cur, cid, pid).notes == ["corrected by hand"]


def test_assistant_sweep_keeps_a_message_sent_while_it_ran(cur, session, monkeypatch):
    async def fold(kobold, m: AssistantMemory, s, card, persona, **k):
        live = main._load(cur.sessions, s.id, ChatSession)
        live.messages.append(ChatMessage(role="user", content="sent during the sweep"))
        main._save(live, cur.sessions)
        m.notes = ["finish the migration"]
        m.swept_sessions[s.id] = 3
        return True

    monkeypatch.setattr(main.assistant_memory, "fold", fold)
    asyncio.run(main.sweep_session(session.id, cur=cur))
    saved = main._load(cur.sessions, session.id, ChatSession)
    assert saved.messages[-1].content == "sent during the sweep"
    assert saved.memory.last_summarized_index == 3
    assert main._load_assistant_memory(cur, *_pair(cur, session)).notes == ["finish the migration"]
