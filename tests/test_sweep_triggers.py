"""Ending a chat saves it to long-term memory, and the server makes sure it happens.

The idle sweep used to be a browser timer, so a closed tab, a reload or a restart
before it fired left the chat's recent turns out of long-term memory. Now a finished
turn arms a timer on the server (_schedule_sweep), a sweep that gives way to a reply
runs again, and startup catches up on recent chats left unswept.

    .venv/bin/python -m pytest -q tests/test_sweep_triggers.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

import main
from models.schema import ChatMessage, ChatSession
from test_chat_stream import _turn, cur, model, session  # noqa: F401 — fixtures


@pytest.fixture()
def folds(monkeypatch):
    """Record which sessions the sweep folded; the fold marks everything as read."""
    seen: list[str] = []

    async def fold(kobold, mem, s, card, persona, upto=None, **k):
        seen.append(s.id)
        mem.swept_sessions[s.id] = len(s.messages) if upto is None else upto
        return True

    monkeypatch.setattr(main.assistant_memory, "fold", fold)
    return seen


def _mark(cur, s) -> int:
    return main._load_assistant_memory(cur, s.character_id, s.persona_id).swept_sessions.get(s.id, 0)


def test_a_finished_turn_arms_the_servers_idle_sweep(cur, session, model):
    async def go():
        await _turn(cur, session)
        t = main._sweep_timers.get(session.id)
        assert t is not None and not t.done()     # waiting, on the server, not the browser
        t.cancel()
    asyncio.run(go())


def test_the_idle_sweep_folds_the_whole_chat_recent_turns_included(cur, session, folds):
    async def go():
        main._schedule_sweep(cur.user.id, session.id, delay=0)
        await asyncio.gather(*list(main._sweep_tasks))
    asyncio.run(go())
    assert folds == [session.id]
    assert _mark(cur, session) == len(session.messages)    # not len - keep_recent


def test_rearming_replaces_a_waiting_timer(cur, session, folds):
    async def go():
        main._schedule_sweep(cur.user.id, session.id, delay=30)
        first = main._sweep_timers[session.id]
        main._schedule_sweep(cur.user.id, session.id, delay=0)
        await asyncio.sleep(0)
        assert first.cancelled()
        await asyncio.gather(*list(main._sweep_tasks), return_exceptions=True)
    asyncio.run(go())
    assert folds == [session.id]                             # once, not twice


def test_no_sweep_for_a_user_deleted_meanwhile(cur, session, folds, monkeypatch):
    monkeypatch.setattr(main, "_user_by_id", lambda uid: None)

    async def go():
        main._schedule_sweep(cur.user.id, session.id, delay=0)
        await asyncio.gather(*list(main._sweep_tasks))
    asyncio.run(go())
    assert folds == []


def test_a_sweep_that_gave_way_to_a_reply_runs_again(cur, session, monkeypatch):
    calls = []

    async def sweep(c, sid, kobold=None):
        calls.append(sid)
        if len(calls) == 1:
            raise main._MemoryYielded()
        return {"ok": True, "swept": True}

    monkeypatch.setattr(main, "_sweep", sweep)
    monkeypatch.setattr(main, "SWEEP_RETRY_S", 0)

    async def go():
        out = await main._sweep_job(cur, session.id)
        assert out["swept"] is False and "runs again" in out["reason"]
        await asyncio.gather(*list(main._sweep_tasks))
    asyncio.run(go())
    assert calls == [session.id, session.id]


def test_the_endpoint_still_reports_the_result(cur, session, folds):
    out = asyncio.run(main.sweep_session(session.id, cur=cur))
    assert out == {"ok": True, "swept": True}


def test_catchup_picks_recent_chats_with_unswept_user_turns(cur, session, folds):
    old = ChatSession(character_id=session.character_id, persona_id=session.persona_id,
                      updated_at=(datetime.now() - timedelta(days=30)).isoformat())
    old.messages = [ChatMessage(role="char", content="hi"), ChatMessage(role="user", content="x")]
    greeting_only = ChatSession(character_id=session.character_id, persona_id=session.persona_id)
    greeting_only.messages = [ChatMessage(role="char", content="hello")]
    for s in (old, greeting_only):
        main._save(s, cur.sessions)

    assert main._unswept_recent(cur) == [session.id]         # not the old one, not a greeting
    asyncio.run(main.sweep_session(session.id, cur=cur))
    assert main._unswept_recent(cur) == []                   # swept: nothing left to catch up



def test_nothing_new_or_nobody_spoke_costs_no_backend_call(cur, session, folds, monkeypatch):
    """Every chat that is left or closed asks for a save; most have nothing new."""
    probed = []

    async def probe(st):
        probed.append(1)
        return 8192

    monkeypatch.setattr(main, "_context_tokens", probe)
    greeting_only = ChatSession(character_id=session.character_id, persona_id=session.persona_id)
    greeting_only.messages = [ChatMessage(role="char", content="hello")]
    main._save(greeting_only, cur.sessions)

    assert asyncio.run(main.sweep_session(greeting_only.id, cur=cur))["swept"] is False
    assert asyncio.run(main.sweep_session(session.id, cur=cur))["swept"] is True
    assert asyncio.run(main.sweep_session(session.id, cur=cur))["swept"] is False  # again
    assert folds == [session.id] and probed == [1]


# --- Memory Phase 7 #19: the episode, and context pressure ------------------------------------

def test_the_end_of_an_episode_saves_her_other_chats_too(cur, session, folds):
    """A quiet gap ends the episode for the bartender: her other chats with unread turns
    (voice bursts in the desktop window) are saved with it; one still in use is left."""
    voice = ChatSession(character_id=session.character_id, persona_id=session.persona_id,
                        messages=[ChatMessage(role="user", content="play some jazz"),
                                  ChatMessage(role="char", content="Playing.")])
    busy = ChatSession(character_id=session.character_id, persona_id=session.persona_id,
                       messages=[ChatMessage(role="user", content="still talking here")])
    other_card = ChatSession(character_id="someone-else", persona_id=session.persona_id,
                             messages=[ChatMessage(role="user", content="not hers")])
    for s in (voice, busy, other_card):
        main._save(s, cur.sessions)

    async def go():
        main._schedule_sweep(cur.user.id, busy.id, delay=60)      # in use: its own timer waits
        main._schedule_sweep(cur.user.id, session.id, delay=0)
        for _ in range(20):
            await asyncio.sleep(0.01)
        main._sweep_timers[busy.id].cancel()
        await asyncio.gather(*list(main._sweep_tasks), return_exceptions=True)
    asyncio.run(go())
    assert folds == [session.id, voice.id]


def test_context_pressure_folds_below_the_message_count(cur, session, monkeypatch):
    calls = []

    async def fold(kobold, mem, s, card, persona, upto=None, **k):
        calls.append(upto)
        return True
    monkeypatch.setattr(main.assistant_memory, "fold", fold)
    st = main._load_settings(cur)
    st.summarize_after, st.keep_recent = 100, 2                     # far from the count trigger
    main._save_settings(cur, st)
    asyncio.run(main._maintain_memory(cur, session.id))
    assert calls == []                                                # no pressure: no fold
    asyncio.run(main._maintain_memory(cur, session.id, pressure=True))
    assert calls == [len(session.messages) - 2]                       # pressure: fold all but the recent turns


def test_a_turn_that_had_to_drop_turns_asks_for_a_fold(cur, session, model, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "_spawn_memory_job", lambda cur, sid, pressure=False: seen.append(pressure))

    async def small(st):
        return 600                                   # a window too small for the whole chat
    monkeypatch.setattr(main, "_context_tokens", small)
    session.messages += [ChatMessage(role="user" if i % 2 else "char", content="word " * 60) for i in range(12)]
    main._save(session, cur.sessions)

    async def go():
        await _turn(cur, session)
        for t in list(main._sweep_timers.values()):
            t.cancel()
    asyncio.run(go())
    assert seen == [True]
