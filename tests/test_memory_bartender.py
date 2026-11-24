"""Memory belongs to the bartender (Memory Phase 7, #16): shared user facts, her own
instructions and notes, persona as a tag, and the one-time merge of the old pairing files.

    .venv/bin/python -m pytest -q tests/test_memory_bartender.py
"""
from __future__ import annotations

import json

import main
from models.schema import AssistantMemory, CharacterCard, ChatMessage, ChatSession, MemoryFact, Persona
from test_chat_stream import cur  # noqa: F401 — fixture


def _cards(cur):
    mira, rex = CharacterCard(name="Mira"), CharacterCard(name="Rex")
    main._save(mira, cur.cards)
    main._save(rex, cur.cards)
    return mira, rex


def test_facts_are_shared_instructions_and_notes_are_hers(cur):
    mira, rex = _cards(cur)
    pid = main._default_persona(cur).id
    m = main._load_assistant_memory(cur, mira.id, pid)
    m.facts.append(MemoryFact(text="Brian lives in Porto", origin="auto"))
    m.directives.append(MemoryFact(text="Keep answers short"))
    m.directives.append(MemoryFact(text="Use 24-hour times", scope="all"))
    m.notes = ["The DNS switch is still to do"]
    main._save_assistant_memory(cur, m)

    r = main._load_assistant_memory(cur, rex.id, pid)
    assert [f.text for f in r.facts] == ["Brian lives in Porto"]                 # shared
    assert [d.text for d in r.directives] == ["Use 24-hour times"]               # only the one for all
    assert r.notes == []                                                          # Mira's thread is hers
    again = main._load_assistant_memory(cur, mira.id, pid)
    assert {d.text for d in again.directives} == {"Keep answers short", "Use 24-hour times"}


def test_switching_persona_no_longer_hides_her_instructions(cur):
    mira, _ = _cards(cur)
    work = Persona(name="Brian at work")
    main._save(work, cur.personas)
    home = main._default_persona(cur).id
    m = main._load_assistant_memory(cur, mira.id, home)
    m.directives.append(MemoryFact(text="Keep answers short"))
    m.facts += [MemoryFact(text="Brian is vegetarian"), MemoryFact(text="Brian's desk is 4B", persona_id=work.id)]
    main._save_assistant_memory(cur, m)
    as_home = main._load_assistant_memory(cur, mira.id, home)
    as_work = main._load_assistant_memory(cur, mira.id, work.id)
    assert [d.text for d in as_home.directives] == [d.text for d in as_work.directives] == ["Keep answers short"]
    assert [f.text for f in as_home.facts] == ["Brian is vegetarian"]            # the work fact is tagged
    assert {f.text for f in as_work.facts} == {"Brian is vegetarian", "Brian's desk is 4B"}
    main._save_assistant_memory(cur, as_home)                                     # saving a view keeps hidden facts
    assert len(main._load_assistant_memory(cur, mira.id, work.id).facts) == 2


def test_old_pairing_files_and_pinned_facts_merge_once(cur):
    mira, rex = _cards(cur)
    p1, p2 = Persona(name="A"), Persona(name="B")
    for old_pid, directive, fact, note in ((p1.id, "Keep answers short", "Brian lives in Porto", "Finish the DNS"),
                                           (p2.id, "Keep answers short", "Brian has a cat", "")):
        (cur.assistant / f"{mira.id}__{old_pid}.json").write_text(AssistantMemory(
            character_id=mira.id, persona_id=old_pid, directives=[MemoryFact(text=directive)],
            facts=[MemoryFact(text=fact)], notes=[note] if note else [],
            swept_sessions={"s1": 4}).model_dump_json())
    s = ChatSession(character_id=mira.id, persona_id=p1.id, messages=[ChatMessage(role="user", content="hi")])
    s.memory.pinned = ["Afraid of deep water"]
    main._save(s, cur.sessions)

    m = main._load_assistant_memory(cur, mira.id, p1.id)
    assert [d.text for d in m.directives] == ["Keep answers short"]              # deduped across personas
    assert {f.text for f in m.facts} == {"Brian lives in Porto", "Brian has a cat", "Afraid of deep water"}
    assert m.notes == ["Finish the DNS"] and m.swept_sessions == {"s1": 4}
    assert main._load(cur.sessions, s.id, ChatSession).memory.pinned == []         # moved, not copied
    assert (cur.assistant / f"{mira.id}__{p1.id}.json").exists()                  # old files kept
    # Rex never had memory of his own, but knows the user's facts at once.
    assert {f.text for f in main._load_assistant_memory(cur, rex.id, p2.id).facts} >= {"Brian lives in Porto"}


def test_deleting_a_bartender_or_a_persona_forgets_theirs(cur):
    mira, _ = _cards(cur)
    work = Persona(name="Brian at work")
    main._save(work, cur.personas)
    m = main._load_assistant_memory(cur, mira.id, work.id)
    m.notes = ["Mira's thread"]
    m.facts += [MemoryFact(text="Brian is vegetarian"), MemoryFact(text="desk 4B", persona_id=work.id)]
    main._save_assistant_memory(cur, m)
    main._forget_pairings(cur, persona_id=work.id)
    shared = json.loads((cur.assistant / "_shared.json").read_text())
    assert [f["text"] for f in shared["facts"]] == ["Brian is vegetarian"]
    main._forget_pairings(cur, character_id=mira.id)
    assert not (cur.assistant / f"{mira.id}.json").exists()


def test_the_modal_keeps_a_line_once_and_carries_each_instructions_scope(cur):
    import asyncio
    mira, rex = _cards(cur)
    pid = main._default_persona(cur).id
    asyncio.run(main.put_assistant_memory({
        "character_id": mira.id, "persona_id": pid,
        "directives": [{"text": "Use 24-hour times", "scope": "all"}, {"text": "Keep answers short"}],
        "facts": [{"text": "Brian lives in Porto"}, {"text": "brian lives in porto"}]}, cur=cur))
    m = main._load_assistant_memory(cur, mira.id, pid)
    assert [f.text for f in m.facts] == ["Brian lives in Porto"]
    assert [d.text for d in main._load_assistant_memory(cur, rex.id, pid).directives] == ["Use 24-hour times"]
